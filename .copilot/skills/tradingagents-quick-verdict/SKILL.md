# TradingAgents · Quick Verdict

Run the full dual-frame (aggressive + conservative) persona analysis on a
single ticker and return a compact tier/PT/compression verdict. This is the
"ask a question, get a tradeable answer" entry point.

Wraps `run_copilot_matrix.py --tickers TICKER` so you get the full pipeline:
all four analysts (market · news · fundamentals · sentiment), bull/bear
researcher debate, trader, risk-mgmt, portfolio manager — under BOTH
aggressive and conservative personas. Then parses `verdict_ledger.json`
and prints the canonical tier.

## When to invoke this skill

The user is asking for a tradeable verdict on a ticker — should I buy, what's
the PT, what's the conviction. Recognise phrases like:

- "Quick verdict on NVDA"
- "What does TradingAgents think about INTC?"
- "Should I buy TJX?"
- "Run the matrix on CRWV"
- "Dual-frame check on MU"
- "What's the tier on GS?"

This is slower than the other quick_* skills (~3-5 minutes per call). For
raw data lookups (news / fundies / technicals / sentiment), prefer the
single-domain skills which return in seconds.

## How to use

```bash
.venv/bin/python scripts/quick_verdict.py TICKER \
    [--max-parallel 2] \
    [--no-chronos] \
    [--no-iv-surface] \
    [--date YYYY-MM-DD] \
    [--json]
```

- `TICKER`: stock symbol
- `--max-parallel`: thread pool size (default 2 = aggressive + conservative
  in parallel)
- `--no-chronos`: skip the 90-day Chronos forecast overlay (~30s faster)
- `--no-iv-surface`: skip the IV-surface scoring (~10s faster)
- `--date`: trade date YYYY-MM-DD (default today)
- `--json`: emit JSON instead of markdown (for programmatic use)

## Output

Markdown with:
- Tier (A / B / C / VETO / —) per the canonical rule
- Classification (PICK / VETOED)
- Current price (Polygon `/prev`)
- Aggressive: rating + PT
- Conservative: rating + PT
- PT compression % (A < 5%)
- Implied upside %
- Path to the full per-ticker drilldown markdown

## Tier rule (canonical · must stay in sync with 4 other implementations)

```
classification == "PICK" AND conservative_pt is None        → C
classification == "PICK" AND compression < 5.0%             → A
classification == "PICK" AND compression >= 5.0%            → B
classification == "VETOED"                                  → VETO
otherwise                                                   → —
```

## Communication style

When summarising for the user:

- **Lead with the spoken summary** (one English sentence):
  - "NVDA → C tier (aggressive-only thesis). Buy/PT $230 from aggressive,
    conservative declined to model. +5% implied upside, premium-rich chain."
- **Always include current price** alongside PTs (PT without price is
  meaningless).
- **Frame PTs as scenario inputs, not exit triggers**: cons PT is "ring the
  register" / trim level; agg PT is "stretch case" / full upside.
- **No Greek letters** outside option-contract specs.
- **Don't recommend a contract** from this skill alone — that's
  `tradingagents-portfolio-advisor`'s job. Defer with: "Pass this to
  portfolio_check / portfolio_trade_tickets for a sized trade ticket."
- **If tier is VETO**: lead with why (cons rating / cons PT below price).
  Tier-A on a stale screen ≠ same conviction as a fresh tier-A.

## Empirical tier ROI (long-call, cross-run)

| Tier | Avg long-call ROI | Notes |
|---|---:|---|
| A | −35% | Equity-only setups; 5-7% modeled upside doesn't clear premium |
| B | +108% ⭐ | Cons engaged but skeptical → premium clears the wider gap |
| C | +41% | Aggressive-only thesis; stay at starter size |

## Install (once per host)

```bash
ln -s "$(pwd)/.copilot/skills/tradingagents-quick-verdict" \
    ~/.copilot/skills/tradingagents-quick-verdict
```
