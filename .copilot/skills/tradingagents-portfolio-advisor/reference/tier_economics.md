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

## Empirical ROI (long-call default strategy, Δ 0.55)

| Tier | Avg ROI | Interpretation |
|---|---:|---|
| A | **−35%** | Tight aggressive/cons agreement → modeled upside is 5–7%, doesn't clear long-call premium. **Equity-only territory; long calls underperform.** |
| B | **+108%** ⭐ | Cons engaged but skeptical → wider modeled aggressive PT, premium clears it. **The workhorse tier — earn most of your size here.** |
| C | **+41%** | Aggressive-only thesis (cons declined to model) → wider PT bands, smaller agreement. **Stay at starter size.** |

Source: `runs/index.db` cross-run aggregation as of late April 2026.

## Sizing implications

These numbers drive `policy.json::tier_sizing`:

```json
"A": { "starter_pct": 0.04, "max_pct": 0.06 },
"B": { "starter_pct": 0.04, "max_pct": 0.08 },
"C": { "starter_pct": 0.02, "max_pct": 0.04 }
```

Reasoning:

- **Tier A** gets a normal starter (4%) but a low max (6%). The empirical
  −35% ROI says don't lean in; the long-call structure is the wrong vehicle.
  Either size to equity expectations or use a different structure (e.g.,
  shorter-dated lower-Δ calls if you must).
- **Tier B** gets the largest max (8%). The +108% ROI history justifies
  scaling up on confirmation. This is where alpha comes from.
- **Tier C** gets a small starter (2%) and small max (4%). Aggressive-only
  thesis without cons corroboration is higher-variance; the +41% average
  hides wide dispersion.

## Anti-patterns

- ❌ Sizing Tier A largest because "tight agreement = high conviction".
  Empirically wrong for the long-call structure. Tight agreement means the
  modeled upside is only 5–7%, which doesn't pay long-call premium.
- ❌ Treating Tier C as "almost B". Cons declining to model is a signal of
  thesis weakness, not just frame asymmetry.
- ❌ Treating VETO as binary "no signal". The cons frame's specific concerns
  (cited by name in `cells/conservative/<T>/summary.md`) are themselves
  signal — read the rationale.
