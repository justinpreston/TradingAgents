# Tier economics

Tier A/B/C is **derived per-row** from the matrix verdict ledger via:

```python
def _tier(row):
    if row['classification'] != 'PICK':
        return 'VETO' if row['classification'] == 'VETOED' else '—'
    if row.get('conservative_pt') is None:
        return 'C'   # cons declined to model
    if row.get('pt_compression_pct') is not None and row['pt_compression_pct'] < 5.0:
        return 'A'   # tight dual-frame agreement
    return 'B'       # cons engaged but skeptical
```

## Realized ROI (long-call default strategy, Δ 0.55)

Correction: the prior A=−35%, B=+108%, C=+41% figures were **modeled**
gain-if-PT-hits-at-expiry estimates, assuming a 100% PT-hit rate. They were
not realized returns and are superseded by the realized, exit-aware backtest in
`runs/backtest_exits_2026-06-26.json` plus the hold baseline in
`runs/backtest_2026-06-26.json` (marked 2026-06-26, n=92 deduped picks).

Overall selection alone is roughly a coin flip: buy-and-hold was **50%**
profitable with median option ROI of about **+0.7%**. The edge is **exit
discipline**: selling into the aggressive price target raised median ROI to
**+13.8%** with a **57%** win rate; the policy-style half-cons / half-aggr
scale-out was close behind at **+12.6%** median with the highest win rate
(**58%**).

| Tier | Hold baseline | Exit-aware result | Interpretation |
|---|---:|---:|---|
| A | **−5.2%** median / **50%** win | Cons-PT exit: **+21.6%** median / **68%** win; aggr-PT exit: **+19.0%** / **64%** | Tier A is bad only if held. With disciplined exits it is the most reliable tier. |
| B | **+15.2%** median / **54%** win | Cons-PT cut: **0%** median / **31%** win; aggr-PT exit roughly preserves the hold profile | Small sample (n=13). Do **not** cut B at the conservative PT; let it run to the aggressive target. |
| C | **0%** median / **49%** win | Aggr-PT exit: **+12.5%** median / **54%** win | Aggressive-only theses need the aggressive-target exit; otherwise the median is flat. |

PT-hit rates from the same realized packet: A cons **68%** / aggr **55%**;
B cons **69%** / aggr **23%**; C aggr **30%**.

## Sizing implications

Current `policy.json::tier_sizing` remains:

```json
"A": { "starter_pct": 0.04, "max_pct": 0.06 },
"B": { "starter_pct": 0.04, "max_pct": 0.08 },
"C": { "starter_pct": 0.02, "max_pct": 0.04 }
```

Reasoning for the advisor:

- **Tier A** should no longer be treated as the long-call underperformer.
  The realized backtest says it is the most reliable tier **with** exit
  discipline: trim 50% at the conservative PT and sell the rest at the
  aggressive PT.
- **Tier B** still deserves room to run, but the reason changed. Conservative
  PT exits hurt; the advisor should not recommend trimming B merely because
  the conservative target was touched.
- **Tier C** remains a starter-size, higher-variance tier. Its realized edge
  comes from selling at the aggressive PT, not from holding indefinitely.
- The sizing percentages above have **not** been changed by this correction.
  Given the Tier A finding, sizing may warrant user review, but this reference
  doc does not edit `runs/portfolio/policy.json`.

## Anti-patterns

- ❌ Treating Tier A as "equity-only" or structurally bad for long calls.
  That was based on stale modeled numbers. Realized data says Tier A works
  well when the conservative/aggressive target exits are enforced.
- ❌ Treating Tier C as "almost B". Cons declining to model is a signal of
  thesis weakness, not just frame asymmetry.
- ❌ Treating VETO as binary "no signal". The cons frame's specific concerns
  (cited by name in `cells/conservative/<T>/summary.md`) are themselves
  signal — read the rationale.
