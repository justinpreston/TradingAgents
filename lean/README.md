# TradingAgents LEAN integration

LEAN is the **hands** for a workflow where the weekly TradingAgents run remains
the **brain**. The realized backtests show why this split matters:
buy-and-hold on selected long-call picks was roughly a coin flip
(50% profitable, ~+0.7% median option ROI), while disciplined exits changed
the result materially. Selling into the aggressive price target lifted the
median to **+13.8%** with a **57%** win rate, and the half-conservative /
half-aggressive scale-out was close behind at **+12.6%** median with the
highest win rate (**58%**). Source: `runs/backtest_exits_2026-06-26.json`;
visual packet: `runs/exit_discipline_packet_2026-06-26.html`.

## Architecture

```text
Weekly TradingAgents run (brain)
  Friday screener + NEW-only matrix + same-day options refresh
        │
        ▼
lean/signals.json
  generated from options_overlay.json + policy.json
  approved:false by default; user flips selected rows to true
        │
        ▼
LEAN algorithm (hands)
  entry + tier-specific exit discipline
        │
        ▼
Broker
  paper first, then capped live
```

The approval gate is intentional. The weekly run produces candidates and
structures; the user decides which rows become executable. LEAN should not
re-screen, re-rank, or bypass the cadence in `CLAUDE.md`.

## The `signals.json` bridge

Generate LEAN-ready signals from a completed matrix run after the same-day
options refresh:

```bash
.venv/bin/python scripts/export_lean_signals.py \
    --matrix-run runs/<matrix_id> \
    --policy-file runs/portfolio/policy.json \
    --output lean/signals.json
```

Each signal defaults to `"approved": false`. Review the rows, then edit only
the trades you want LEAN to execute to `"approved": true`. See
`lean/signals.schema.md` for the field reference; do not duplicate that schema
here.

## Exit-discipline rules

These rules encode the realized edge from `runs/backtest_exits_2026-06-26.json`
and `runs/backtest_2026-06-26.json` (marked 2026-06-26, n=92 deduped picks).

| Tier | LEAN exit rule | Realized justification |
|---|---|---|
| A | **Scale out:** if underlying high ≥ conservative PT, sell 50%; if high ≥ aggressive PT, sell the rest. | Holding Tier A was bad (median **−5.2%**, **50%** win), but disciplined exits made it the most reliable tier: conservative-PT exit median **+21.6%**, **68%** win; aggressive-PT exit median **+19.0%**, **64%** win. PT-hit rates: conservative **68%**, aggressive **55%**. |
| B | **Let it run:** ignore conservative PT; if underlying high ≥ aggressive PT, sell 100%. | Small sample (n=13). Holding had median **+15.2%**; cutting at conservative PT hurt (median **0%**, **31%** win). Conservative PT was hit often (**69%**) but was not the edge; aggressive PT hit rate was **23%**. |
| C | **Trim at aggressive PT:** if underlying high ≥ aggressive PT, sell 100%. | Holding was flat (median **0%**). Aggressive-PT exit improved results to median **+12.5%**, **54%** win, with a **30%** aggressive PT-hit rate. |
| All | **Risk controls:** stop at option premium −40%; time-stop at ≤21 DTE. | These keep LEAN from turning an exit-discipline edge into unmanaged option decay. |

## Staging plan

No shortcuts to live money:

1. **LEAN backtest replay.** Replay historical `signals.json` snapshots and
   confirm the algorithm reproduces the realized exit discipline result,
   especially the roughly **+13.8%** median aggressive-PT exit behavior.
2. **Paper trade one full weekly cycle.** Use IBKR or Tradier paper trading.
   Watch fills on thin / low-open-interest chains; KLIC is a real example of
   why quoted option marks are not the same as executable liquidity.
3. **Live, capped.** Start with half starter size and one tier at a time.
   Because this account is being used to grow out of high-interest debt,
   capital preservation matters more than proving the automation quickly.

## Infra choices

| Decision | Option | Tradeoff |
|---|---|---|
| Runtime | **QuantConnect Cloud** | Managed data/runtime and fastest path to live trading. Less control, more platform dependency. |
| Runtime | **Local LEAN CLI** | Free and self-hosted. More setup, data, scheduling, and ops burden. |
| Broker | **IBKR** | Best fit for options fills and serious live execution. Setup is heavier. |
| Broker | **Tradier** | Simpler API and faster to wire. Execution quality and account features may be less robust. |

Recommendation: use QuantConnect Cloud + IBKR if the priority is getting to a
safe paper-to-live path quickly. Use local LEAN CLI if keeping infrastructure
free and self-controlled is more important. The broker/runtime choice is still
the user's call.

## How to run

LEAN CLI and LEAN data are not installed in this repo yet. Expected shape once
installed:

```bash
# One-time, outside the TradingAgents Python dependencies
pip install lean

# From the repo root, initialize/configure LEAN as needed
lean --version

# Backtest the LEAN project
cd lean/algorithm
lean backtest

# After backtest + paper validation, launch live
lean live
```

Project layout:

```text
lean/
├── README.md
├── signals.json              # generated bridge file, user-approved rows only
├── signals.schema.md         # signal field reference
└── algorithm/
    ├── TradingAgentsAlgorithm.py
    ├── SignalData.py
    ├── config.json
    └── README.md
```

## Open items / caveats

- Backtests are marked-to-now, not held-to-expiry.
- Exit means option close on the trigger day.
- Signals use daily closes/highs only; no intraday trigger simulation.
- No slippage or commissions are included.
- Tier B has a small sample (n=13), so treat its rule as promising but less
  statistically settled than Tier A/C.
- `runs/portfolio/policy.json` sizing has **not** been changed. The Tier A
  realized result may warrant revisiting sizing, but that is intentionally left
  for the user to decide.
