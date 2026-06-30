# TradingAgents LEAN Algorithm

This project consumes a TradingAgents `signals.json` manifest and executes only approved long-call signals. Put `signals.json` in this LEAN project folder, or pass a different location with the `signals-path` parameter.

## Setup

Install the LEAN CLI, then run from the repository root or this project folder:

```bash
pip install lean
lean login
lean backtest lean/algorithm
```

For live trading, use:

```bash
lean live lean/algorithm
```

Brokerage, account, and data-provider wiring for IBKR, Tradier, QuantConnect Cloud, etc. is configured by `lean live`; it is intentionally outside this algorithm code.

## Parameters

- `signals-path`: path to the TradingAgents manifest, default `signals.json`.
- `stop-loss-pct`: fallback stop loss if a signal omits one, default `-0.40`.
- `time-stop-dte`: fallback days-to-expiry time stop, default `21`.

## Exit behavior

The algorithm checks exits daily near market close. Tier A sells half at the conservative price target and the rest at the aggressive target. Tier B and C ignore the conservative target and sell all at the aggressive target. All tiers also exit fully on the option stop loss or time stop.

See the top-level `lean/README.md` for overall bridge architecture.
