# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Coding-agent runbook — TradingAgents

> **Read this first.** This file is the canonical operational guide for Claude
> Code, GitHub Copilot, Cursor, or any other agent working in this repo. It
> describes the locked-in pipeline cadence, command surface, and the invariants
> that have historically broken when ignored.
>
> If you update this file, also update `.github/copilot-instructions.md`
> (which is a pointer plus the most critical must-knows).

---

## TL;DR — running the pipeline

```bash
# Weekly Friday tick — auto-detects what's new vs catalog and prints next-step commands
.venv/bin/python scripts/weekly_workflow.py --top 25

# Reuse an existing screener output (e.g. you already ran it earlier today)
.venv/bin/python scripts/weekly_workflow.py \
    --use-screener-run runs/screener_<id> --top 25

# Auto-launch matrix on the NEW tickers only (skips wasteful re-screening)
.venv/bin/python scripts/weekly_workflow.py --top 25 --chain --chain-top 5

# Same-day options refresh before any entry (auto-run Fridays 14:00 via launchd;
# manual form below). Refreshes ALL of today's matrix runs + rebuilds the packet:
.venv/bin/python scripts/friday_options_refresh.py
# Single-run manual form:
.venv/bin/python scripts/build_options_overlay.py \
    --matrix-run runs/<matrix_id> --strategy-mode long-call

# Friday decision packet — one-page ranked tickets across all of today's tier runs
.venv/bin/python scripts/build_friday_packet.py --date <YYYY-MM-DD>

# Approve a LEAN signal (the ONLY sanctioned way to flip approved:true)
.venv/bin/python scripts/approve_lean_signal.py --list
.venv/bin/python scripts/approve_lean_signal.py --id <TICKER-YYYY-MM-DD>

# Optional: news enrichment (sentiment + theme tags) on the screener output
# Default scorer is now FinBERT (~3× more discriminating than keyword on real
# headlines). Auto-falls-back to keyword if torch+transformers not installed.
.venv/bin/python scripts/build_news_enrichment.py \
    --screener-run runs/<screener_id>
# Force the zero-dep keyword scorer:
.venv/bin/python scripts/build_news_enrichment.py \
    --screener-run runs/<screener_id> --scorer keyword
# A/B test keyword vs FinBERT side-by-side:
.venv/bin/python scripts/build_news_enrichment.py \
    --screener-run runs/<screener_id> --scorer both

# Portfolio advisor — per-position trade tickets + portfolio-wide queue
.venv/bin/python scripts/portfolio_health.py \
    --positions-file runs/portfolio/positions.json \
    --policy-file runs/portfolio/policy.json
# (See "Portfolio advisor" section below.)

# Tests — see the "Testing" section for baseline + known flake
.venv/bin/python -m pytest tests/ -x -q
```

---

## The locked-in cadence (revised 2026-07-01 — Friday pre-market)

User's explicit decision (confirmed 2026-07-01; do not re-litigate without
confirmation): **picks must exist early enough to trade the same Friday, at
the user's discretion (Friday or Monday entry).** The old Friday-17:00
post-close schedule made Friday entries impossible and conferred zero data
advantage — the screener provably consumes Thursday EOD bars either way
(`screener/universe.py::latest_trading_day()` = yesterday).

| When (ET) | What | How |
|---|---|---|
| **Friday 06:30** | Full auto-chain: screen all tiers → diff → matrix NEW tickers → overlays | launchd `com.tradingagents.weekly.plist` → `run_weekly_all_tiers.py --top 25 --chain --chain-top 5 --chain-max-parallel 5` |
| **Friday ~07:45–09:10** | Review the Friday decision packet | `scripts/build_friday_packet.py` output in `runs/friday_packet_<date>/` |
| **Friday 14:00** | Same-day options refresh (all of today's matrix runs) + packet rebuild | launchd `com.tradingagents.friday-refresh.plist` → `scripts/friday_options_refresh.py` |
| **Friday afternoon / Monday** | User approves signals + executes at discretion | `scripts/approve_lean_signal.py --id <TICKER-DATE>`; manual Fidelity or LEAN paper |

**Override triggers** (re-run outside cadence):
- Earnings on an active pick → re-matrix that single ticker.
- VIX > 25 or SPX −5%/week → full re-screen (regime shift).
- Quarter-end → full refresh (screen + matrix + options).

Install/refresh both launchd jobs with:
```bash
cp scripts/launchd/com.tradingagents.weekly.plist ~/Library/LaunchAgents/
cp scripts/launchd/com.tradingagents.friday-refresh.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tradingagents.weekly.plist
launchctl load ~/Library/LaunchAgents/com.tradingagents.friday-refresh.plist
```
The morning job **does** auto-chain (user decision 2026-07-01: "auto-chain,
review after" — LLM spend is bounded at `--chain-top 5` per tier). Measured
wall-clock (2026-06-26 logs): screeners 0.4–2.9 min/tier; matrix ~21 min/tier
(**~9 min median per cell**, not the 3–5 min previously claimed here); all
post-matrix overlays combined < 60s/tier. Fresh 3-tier end-to-end ≈ 72 min.
Market holidays: the job fires anyway and consumes the prior session's bars —
harmless, but no fresh picks materialize on an exchange holiday.

---

## Pipeline architecture

```
run_screener.py  →  runs/screener_<TS>/{screener.json, top_tickers.txt}
                         │
                         ▼
run_copilot_matrix.py  →  runs/matrix_<TS>_top25/
                            ├ cells/aggressive/<T>/<T>.state.json   (Stage A)
                            └ cells/conservative/<T>/<T>.state.json (Stage B, on promotes)
                            
scripts/build_run_accounting.py  →  same dir, adds:
                            ├ verdict_ledger.{csv,json}     ← AUTHORITATIVE per-ticker results
                            ├ trade_synthesis.md            ← human-readable narrative
                            ├ per_ticker/<T>.md
                            └ current_prices.json

scripts/build_options_overlay.py  →  same dir, adds:
                            └ options_overlay.{md,json}     ← Polygon-pulled options structures

scripts/score_picks_iv_surface.py  →  same dir, adds:
                            └ iv_surface_ranking.{md,json}  ← stockpile-style IV-surface scoring + earnings flag

scripts/build_chronos_overlay.py  →  same dir, adds:
                            └ chronos_overlay.{md,json}     ← Amazon Chronos forecast vs persona PT

scripts/build_html_report.py  →  runs/cross_run_<DATE>/report.html  (cross-run dashboard)

scripts/index_runs.py  →  runs/index.db                     ← SQLite catalog (DERIVED, regenerable)

scripts/weekly_workflow.py  →  orchestrates SCREEN → INDEX → DIFF → CHAIN → REPORT
```

`run_copilot_persona_aligned.py` is the per-ticker sub-runner each matrix cell
shells out to (with `--persona-routing aggressive-aligned` pinned — the
matrix's historical persona→model table; the flag's default `persona-aligned`
is the standalone behavior). Also runnable directly for a quick single-stage
analysis (produces flat `<T>.state.json` files; **does not** produce the
`verdict_ledger.json` + `cells/` structure that
`build_options_overlay.py`/`build_run_accounting.py` consume — for that you
need `run_copilot_matrix.py`). Matrix cells auto-retry once on failure
(`attempt` field + `"<stage>-retry"` events in `events.jsonl`). See the
**Top-level runners** section below for how the `run_*.py` shims relate to
the cadence.

`scripts/weekly_workflow.py --chain` defaults to `--chain-runner run_copilot_matrix.py`
for exactly this reason — feed it any other runner and Phase 4 will warn that
the rest of the cadence can't ingest the output.

`scripts/index_runs.py` detects matrix vs screener runs by the presence of
`verdict_ledger.json` or `screener.json` (not by directory name), so
custom run-ids like `matrix_pipeline_test_<id>` or `matrix_weekly_<ts>_chain`
are indexed correctly.

---

## Top-level runners (`run_*.py`, `compare_runs.py`, `resynthesize_pm.py`)

The repo root holds a fan of single-purpose runner scripts. Only two are
wired into the weekly cadence; the rest are ad-hoc or experimental and a
fresh agent should not promote them into the rhythm without confirmation.

| Script | Role | When to touch |
|---|---|---|
| `run_screener.py` | **Cadence.** Tier-aware universe screen. | Driven by `weekly_workflow.py`. |
| `run_copilot_matrix.py` | **Cadence.** Dual-persona Stage-A → Stage-B matrix; the only runner whose output the rest of the pipeline ingests. | `weekly_workflow.py --chain`; one-off matrix runs. |
| `run_copilot_persona_aligned.py` | **Cadence.** The per-ticker sub-runner every matrix cell shells out to (`SUB_RUNNER`), invoked with `--persona-routing aggressive-aligned` (the matrix's historical persona→model table). Also runnable standalone (default routing `persona-aligned`) — but does **not** write `verdict_ledger.json`. | Matrix cells; ad-hoc single-ticker checks. |
| `run_copilot_aggressive_aligned.py` | Deprecated back-compat shim — `os.execv`s into `run_copilot_persona_aligned.py --persona-routing aggressive-aligned`. | Don't use in new work. |
| `run_copilot_multi.py` | Sequential multi-ticker via Copilot Chat API (any model). | Ad-hoc batches. |
| `run_copilot_opus.py`, `run_copilot_opus_multi.py` | Legacy Opus-via-Copilot launchers. | Kept for back-compat; new work should use the multi variant. |
| `run_opus.py` | Direct Anthropic API (no Copilot proxy). Premium-cost. | Ad-hoc Opus runs with a raw key. |
| `run_github_models.py` | Routes through GitHub Models (OpenAI-compat; falls back to `gh auth token`). | Experimentation across hosted models. |
| `compare_runs.py` | Side-by-side markdown diff of two `runs/<id>/summary.json` files. | Comparing persona / model behaviour. |
| `resynthesize_pm.py` | Re-invoke just the Portfolio Manager against a saved `final_state` — skips the expensive debate. | Iterating on PM behaviour without paying for the upstream graph again. |

**Rule of thumb:** if it isn't `run_screener.py` or `run_copilot_matrix.py`,
it isn't part of the cadence. Don't feed it to
`weekly_workflow.py --chain-runner <other>` — Phase 4 explicitly warns when
you do.

---

## User preferences (drive defaults)

1. **Long calls over multi-leg structures** — *"for ease of trading"*. Single
   fill, no leg management, unbounded upside.
   - Default mode: `--strategy-mode long-call --long-call-delta 0.55` (slightly
     ITM — less time decay than ATM, retains leverage).
   - The other mode (`--strategy-mode tier-driven`: A/B → bull call spreads,
     C → ATM long calls) is still available for comparison/back-test.
2. **Trade recommendations always include current price.** Without it,
   risk/reward and upside are meaningless. Polygon
   `/v2/aggs/ticker/{T}/prev` is the canonical source.
3. **Hybrid storage** — raw JSON/markdown files in `runs/<id>/` are the
   authoritative source of truth (immutable, easy to inspect). The SQLite
   catalog `runs/index.db` is purely derived/regenerable for cross-run
   queries. **Never write authoritative data to `index.db`. Never commit it.**
4. **Cross-run reasoning matters.** When the user asks about a name, check
   `runs/index.db` first via `scripts/index_runs.py --query` to see how often
   it has appeared and at what tier.

---

## Tier A/B/C classification (canonical, must keep in sync)

Tier is **derived per-row**, NOT a stored field. The `classification` column
in `verdict_ledger.json` only takes values `PICK` or `VETOED`.

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

**Single source of truth (since 2026-07-01): `tradingagents/tiers.py`** —
`tier_for_row()` + `TIER_RANK`/`tier_rank()`. All eight call sites
(`build_options_overlay`, `build_chronos_overlay`, `build_html_report`,
`index_runs`, `quick_verdict`, `compare_insider_ablation`, `grounding_audit`,
`portfolio_load_context`) delegate via thin wrappers that preserve per-site
return quirks (`—` vs `None` vs `VETO` for non-picks). Change the threshold in
ONE place. Note: `tier_for_row(suspect_caps_a=True)` additionally caps Tier A
→ B when `pt_quality_flags` is non-empty — five sites use it (overlay,
chronos, indexer, grounding audit, html report); three display/compare sites
don't (quick_verdict, compare_insider_ablation, portfolio_load_context).
`tests/test_tiers.py` enforces wrapper parity.

**Empirical context** (cross-run, indexed in `runs/index.db`):

| Tier | Long-call avg ROI | Notes |
|---|---:|---|
| A | **−35%** | "Equity only" — 5–7% modeled upside doesn't clear long-call premium. |
| B | **+108%** ⭐ | Cons engaged but skeptical → wider modeled aggressive PT, premium clears it. |
| C | **+41%** | Aggressive-only thesis. Stay at starter size. |

---

## Empirical signal findings (realized backtest, n=92, measured 2026-06-26)

From joining `runs/backtest_exits_2026-06-26.json` to the pre-trade signals in
`runs/index.db`. Regenerate with `scripts/backtest_signal_report.py` whenever a
new backtest lands; the standing writeup lives in `docs/RUNS_HISTORY.md`
("Empirical signal correlations"). Medians are realized option ROI. **One
measurement date, ~20-30 per bucket — treat as codified hypotheses under
measurement, re-verify after each backtest cycle.**

**Stronger findings (act on these):**

| Signal | Finding |
|---|---|
| Market-cap tier | Monotonic (Spearman −0.30): mid <10B hold-median +3.5% / 59% win; large +1.6% / 50%; mega >200B **−35.6% / 42%**. The mid-cap screener default is vindicated; haircut mega-tier position sizes. |
| Tenor | 90–180 DTE exit-median **+21.3%**, PT-hit 40% vs >180 DTE +1.3%, PT-hit 26%. Prefer ~3–6 month tenors; long-dated premium wasn't earned back. |
| Exit discipline | Selection alone ≈ coin flip (50% win, +0.7% median hold); selling into the aggressive PT lifts to +13.8% median / 57% win. Already automated in the LEAN exit rules. |

**Weaker findings (tracked hypotheses, don't size on them yet):**

- **Recurrence is a caution flag, not a conviction bonus.** Tickers PICKed in
  3+ matrix runs: hold-median −19.6%, 43% win vs seen-once +0.9% / 50% (+18.9%
  on exit). Repeat appearances have marked underperformers.
- **Chronos `agent_pt_quantile` predicts PT-hit rates monotonically** (q<0.6 →
  42% of aggressive PTs hit; q>0.8 → 24%) — use it to calibrate exit-rule
  expectations. On ROI it is non-monotonic: the q0.6–0.8 band was the worst
  bucket (32% win, −35.7% hold-median); agreement (q<0.6) and
  far-above-cone (q>0.8) both did fine.
- **Modeled upside has zero correlation with realized ROI** (pearson +0.04).
- **Screener composite score does not rank outcomes** (Spearman −0.20 to
  −0.30, non-monotonic terciles). It is a candidate-generation filter only.
- **Weekly veto rate** climbed ~55–60% (late Apr) → 83–93% (late May–June);
  track as a gate-tightness regime metric.

---

## News enrichment (optional, off by default)

`scripts/build_news_enrichment.py` writes `news_enrichment.json` next to a
screener or matrix run with two derived signals per ticker:

1. **Sentiment polarity** in [-1, 1] from a pluggable scorer.
2. **Theme labels** (deterministic regex over 10 catalysts: m_and_a,
   earnings, guidance, regulatory, government_action, leadership,
   litigation, product_launch, analyst_action, capital_action).

**Two scorers, A/B-testable:**
- `--scorer finbert` (**default**, ~3× more discriminating on real headlines):
  ProsusAI/finbert transformer. **Auto-falls-back to keyword with a warning**
  if torch+transformers aren't installed — the default should always
  produce some signal rather than crash. Install: `pip install torch transformers`
  (~1.5-2GB + 440MB model on first call).
- `--scorer keyword` (zero deps): weighted regex over a curated
  positive/negative term list. Fast, deterministic, fully auditable
  (`trigger_terms` lists the rules that fired). Best as a cheap negative
  prefilter (warn / miss / downgrade triggers).
- `--scorer both`: runs both side-by-side, prints discrimination ratio
  (`finbert range / keyword range`). On the 2026-05-06 mega-cap smoke
  test (TXN/GS/KO/NVDA/AVGO), this came out to **3.03×**.

**Wired into weekly_workflow.py** as opt-in Phase 2.5:
```bash
.venv/bin/python scripts/weekly_workflow.py --tier mega --enrich-news
.venv/bin/python scripts/weekly_workflow.py --tier mega --enrich-news --news-scorer both
```

**Three downstream consumers** (all opt-in, no behavioral change without flags):

1. **HTML report chips** — `scripts/build_html_report.py` auto-detects
   `news_enrichment.json` next to the supplied `--screener-run` and renders
   a `News` column on the screener watchlist with sentiment polarity and
   the top-2 themes per ticker (purple chip for `government_action` to
   tag INTC-style theses).

2. **Screener re-rank** — `scripts/build_news_enrichment.py --rerank-screener
   --rerank-alpha 0.10` writes `screener_sentiment_reranked.json` and
   `top_tickers_sentiment_reranked.txt` next to the original screener (which
   is **never** modified). Each candidate's score is multiplied by
   `clamp(1 + alpha * polarity, 0.5, 1.5)`. Default alpha 0.10 is gentle —
   on the 2026-05-06 mega-cap watchlist it preserves the top-5 order while
   tilting scores by ±1-3% per ticker.

3. **Matrix cell injection** — `run_copilot_matrix.py --news-enrichment
   <path>` (auto-passed by `weekly_workflow.py --chain --enrich-news`) sets
   `TRADINGAGENTS_NEWS_ENRICHMENT_PATH` for every cell subprocess. The
   `news_analyst` then prepends a compact "Pre-computed news context for
   <T>: …" prefix to its system message before tool calls. **No
   `AgentState` schema change** — the env-var route keeps the langgraph
   checkpointer schema stable. `run_copilot_persona_aligned.py
   --news-enrichment <path>` does the same for one-off runs.

**Insider transactions** are now bound to `fundamentals_analyst.py`
(Form-4 signals via the yfinance → alpha_vantage vendor fallback chain).
Polygon free-tier doesn't expose insider transactions; the vendor router
in `default_config.py` falls through automatically.

---

## Portfolio advisor (skill + execution-follow-up loop)

A Copilot-CLI skill at `.copilot/skills/tradingagents-portfolio-advisor/`
joins the user's positions ledger to the latest matrix run and emits
**plain-spoken trade tickets**: per-position recommendations + a
portfolio-wide ranked queue. Read-only: never places trades, never edits
`positions.json`. The user manually executes and logs.

### File layout

```
.copilot/skills/tradingagents-portfolio-advisor/
├── SKILL.md                      ← invocation triggers + communication style
└── reference/
    ├── cadence.md                ← pointer to this file's weekly rhythm
    ├── tier_economics.md         ← A:-35% / B:+108% / C:+41% empirical ROI
    └── invariants.md             ← Polygon /prev rule, Δ 0.55, drift framework

scripts/                          ← lives in repo root
├── portfolio_load_context.py     ← canonical context loader (joins positions × matrix)
├── portfolio_check.py            ← per-position trade tickets
├── portfolio_allocation.py       ← sector/basket exposure + --candidate <T> simulator
├── portfolio_trade_tickets.py    ← portfolio-wide ranked queue
├── portfolio_health.py           ← weekly synthesis wrapper (calls all of the above)
└── portfolio_log_action.py       ← append-only writer for trades_log.jsonl

runs/portfolio/                   ← gitignored (under runs/), user-private
├── positions.json                ← user's live book (NEVER auto-edited)
├── positions.example.json        ← reference template
├── positions.schema.md           ← field reference + per-share semantics
├── policy.json                   ← sizing / caps / cash buffer / thematic baskets
├── trades_log.jsonl              ← append-only execution log
└── snapshots/<date>.json         ← weekly portfolio snapshots
```

### The plain-spoken contract

Every ticket carries a `spoken_summary` field — one English sentence saying
what to do and why, leading the rendered output as a `> **bold blockquote**`.
The trade-ticket queue's `## What to do` section is a numbered list of just
these summaries. Generated by `portfolio_check.py::spoken_summary_for_ticket()`.

Example output:
```
## What to do
1. ✂️ Sell 2 of your 5 VIRT contracts (keep 3) — VIRT is trading $51.31, past your $38.00 take-profit.
2. 🌱 Buy 17 × CTRE 40C exp 2026-10-16 @ $2.30/sh (~$3,910 total). Top A-tier pick this week.
   ⚠️ Thin chain — only 72 contracts of open interest. Confirm fill before sizing up.
```

**Communication style** (codified in SKILL.md): lead with the action; plain
English over jargon; specific numbers always (strike/expiry/qty/premium/price)
but explained in one short sentence each; no Greek letters outside contract
specs `(Δ 0.62, IV 0.30, OI 800)`; no walking through derivations. Tone =
experienced colleague in a Slack DM, not a research report.

### Execution follow-up loop

`scripts/portfolio_log_action.py` appends to `runs/portfolio/trades_log.jsonl`
when the user executes a trade. `build_context()` then loads the log and
attaches `recent_actions` to each position's `live` dict.
`recommend_for_position()` reads this and surfaces the most recent action in
the rationale, e.g. *"Last logged action on VIRT: TRIM 2 on 2026-05-09 —
'Took half profits'"* + tags the ticket with `has_recent_log`. This way the
skill follows up on what's been done rather than re-suggesting it.

```bash
# After executing a TRIM trade:
.venv/bin/python scripts/portfolio_log_action.py \
    --ticker VIRT --action TRIM --qty 2 --premium 1.85 \
    --underlying 51.31 --notes "Took half profits"
```

Valid actions: `OPEN`, `CLOSE`, `TRIM`, `ADD`, `ROLL`, `EXIT`, `NOTE`.

**The skill must NOT auto-update `positions.json` after a log entry.** The
log captures intent + execution context; positions.json captures current
state. They intentionally diverge until the user reconciles, and the skill
detects+surfaces that divergence.

### Key invariants

- **Per-share vs per-contract** (the big gotcha): positions schema uses
  `premium_paid_per_share` and `current_mark_per_share` (per-share, what the
  broker shows). Total cost = `qty × 100 × premium_paid_per_share`. Matrix
  overlay's `net_debit_per_share` is per-share; `net_debit_per_contract` is
  total $ for one contract (= per_share × 100). Mixing them double-counts
  or under-counts P&L by 100×. The validator catches the legacy
  `_per_contract` field name with an actionable rename error.
- **Stale ≠ demoted**: a held ticker absent from the latest matrix run is
  "stale" (informational), not "demoted" (high signal). Distinction matters
  for HOLD vs EXIT recommendations.
- **Tier rank ladder** (mirrors `compare_insider_ablation.py`): VETO=0,
  "—"/None=1, C=2, B=3, A=4. Lower rank now → demoted.
- **Priority chain** in `recommend_for_position()`:
  `stop_loss > VETO > demoted > roll > take_profit > promoted > HOLD`.
- **`runs/portfolio/` is gitignored** (because all of `runs/` is). Templates
  in repo are placeholders; user data is local-only.

### Discoverability

Repo-local skills at `.copilot/skills/<name>` are not auto-loaded by the CLI.
Symlink the whole set once to make them discoverable across sessions:
```bash
for s in tradingagents-portfolio-advisor \
         tradingagents-quick-{fundamentals,news,sentiment,technicals,verdict}; do
    ln -s "$(pwd)/.copilot/skills/$s" ~/.copilot/skills/$s
done
```

---

## LEAN execution integration (`lean/`)

Turns approved weekly picks into automated long-call trades. The weekly run is
the **brain** (slow, LLM-driven, picks names + targets); LEAN is the **hands**
(deterministic entry + exit). They are decoupled and talk through ONE artifact:
`lean/signals.json`. Never share a process between them.

```
Weekly run → scripts/export_lean_signals.py → lean/signals.json → LEAN algo → broker
(brain)       (the bridge)                     (user approves)     (hands)     (paper→live)
```

**Why this exists — exit discipline is the edge, not selection.** The realized
backtest (`runs/backtest_exits_2026-06-26.json`, n=92) showed buy-and-hold the
picks ≈ a coin flip (50% win, ~+0.7% median option ROI), while selling into the
aggressive PT lifts the median to **+13.8%** (57% win). The LEAN algo automates
exactly that. Visual writeup: `runs/exit_discipline_packet_2026-06-26.html`.

### The bridge: `scripts/export_lean_signals.py`
Reads one or more matrix runs' `options_overlay.json` (+ `policy.json` for
sizing) and emits `lean/signals.json`. Signals default `approved:false` — the
user flips selected rows to `true` (the human-in-the-loop gate) via
`scripts/approve_lean_signal.py` (the ONLY sanctioned flip path; appends an
audit record to `lean/approvals_log.jsonl`). Regeneration **carries forward**
`approved:true` for matching signal ids **only when the contract is unchanged**
(`occ_symbol` equality) — if an intraday refresh rolled the strike/expiry, the
signal resets to `approved:false` with a warning, so an approval never
silently transfers to a contract the user hasn't reviewed. Mixed snapshot dates across runs warn-and-proceed
(`--strict` restores hard-fail); overlay rows missing legs are skipped with a
warning. `lean/signals.json` and the approvals log are **gitignored** (live
user state; `signals.example.json` stays tracked). Tier C nulls `cons_pt`.
Field reference: `lean/signals.schema.md`. Run:
```bash
.venv/bin/python scripts/export_lean_signals.py \
    --matrix-run runs/matrix_mid_weekly_<ts>_chain \
    --matrix-run runs/matrix_large_weekly_<ts>_chain \
    --output lean/signals.json
```
Tests: `tests/test_export_lean_signals.py` (`.venv/bin/python -m pytest tests/test_export_lean_signals.py -q`).

### The algorithm: `lean/algorithm/`
`TradingAgentsAlgorithm.py` (+ `SignalData.py`, `config.json`, local README).
QC-Cloud-style multi-file. Loads `signals.json`, buys the Δ0.55 calls, and runs
**tier-specific exit discipline** checked daily at close off the underlying's high:

| Rule (by tier) | Behavior |
|---|---|
| `tier_a_take_profit` (A) | sell 50% at cons PT, remainder at aggr PT |
| `tier_b_run` (B) | ignore cons PT; sell 100% at aggr PT (let winners run) |
| `tier_c_trim` (C) | sell 100% at aggr PT |
| universal (all) | stop at option premium −40%; time-stop at ≤21 DTE |

These four rules are **canonical** and must match `export_lean_signals.py`'s
rule mapping and `lean/signals.schema.md`. If you change a threshold, change all
three together (same discipline as the tier-derivation four-way sync).

### Deploying to Relay (self-hosted LEAN fleet)
Live execution runs on the **Relay** repo (`~/Documents/GitHub/Relay`), not QC
Cloud. Relay is **1 node = 1 single-file `main.py`**, so shipping there is a
*port* of `lean/algorithm/` into one file under `strategies/<Name>/main.py`
(Relay idiom: `from AlgorithmImports import *`, `STRATEGY_META`, `_tg_notify`,
daily resolution), loading signals from `/LeanCLI/signals.json`. Node 1 (Aurora,
`192.168.7.230`, IBKR) is the options/IBKR node. Deploy via
`scripts/deploy.sh paper <Strategy>`; go-live is gated by Relay's Phase 8
checklist. Full architecture, staging plan, and broker/infra notes: `lean/README.md`.

### Don'ts
- ❌ Don't let LEAN re-screen, re-rank, or bypass the weekly cadence — it only
  executes `approved:true` rows.
- ❌ Don't auto-flip `approved:false → true` in `signals.json` — that's the
  user's explicit gate.
- ❌ Don't let the bridge and the algo drift on the exit-rule names/semantics.

---

## Quick lookup skills (`tradingagents-quick-*`)

Five sibling skills wrap raw-signal dataflows so a future agent (or the
user) can answer single-ticker questions in seconds instead of re-running
the full LangGraph pipeline. All live in `.copilot/skills/` alongside the
portfolio advisor and symlink into `~/.copilot/skills/`.

| Skill / script | What it does | Typical latency |
|---|---|---|
| `tradingagents-quick-fundamentals` → `scripts/quick_fundamentals.py` | Fundamentals snapshot + insider transactions (yfinance → alpha_vantage fallback chain). | ~5–10s |
| `tradingagents-quick-news` → `scripts/quick_news.py` | Recent headlines for a ticker; no LLM step. | ~3–8s |
| `tradingagents-quick-sentiment` → `scripts/quick_sentiment.py` | Reddit + Stocktwits raw posts, optional FinBERT/keyword polarity. | ~5–10s |
| `tradingagents-quick-technicals` → `scripts/quick_technicals.py` | OHLCV + indicators via the market dataflow. | ~3–8s |
| `tradingagents-quick-verdict` → `scripts/quick_verdict.py` | **Slow path** — shells out to `run_copilot_matrix.py` for one ticker, parses `verdict_ledger.json`, prints Rating / PTs / compression / tier / one-line ticket. | ~3–5 min |

Routing heuristic:
- "What's the read on X?" → `quick-verdict` (full dual-frame verdict on one name).
- "Is X sentiment turning?" / "What's the recent news?" → the matching
  single-dimension quick skill.
- Anything that touches the live book or asks "what should I do with my
  portfolio?" → the portfolio advisor skill, not these.

---

## Subprocess invariants (these have all broken matrix runs historically)

1. **`stdin=subprocess.DEVNULL`** on every child process call. Without it,
   headless / CI invocations hang waiting for input. Fixed in commit
   `50b41e4`. Applies to: `run_copilot_matrix.py`, `weekly_workflow.py::_run()`,
   anything that shells out to a per-ticker runner.
2. **Flag is `--max-parallel`, NOT `--parallel`** on `run_copilot_matrix.py`.
3. **`--stop-on-overweight 0`** for full-coverage matrix runs (otherwise the
   matrix terminates on the first OVERWEIGHT verdict).
4. **`load_dotenv('.env')` with explicit path** (Python 3.13 has a heredoc
   invocation quirk where the implicit search path misses).

---

## Run output anatomy

```
runs/matrix_2026-05-01_top25/
├── verdict_ledger.csv             ← spreadsheet-friendly per-ticker verdicts
├── verdict_ledger.json            ← AUTHORITATIVE, programmatic source
├── trade_synthesis.md             ← human narrative (top picks, asymmetry, tier breakdown)
├── README.md                      ← run metadata
├── current_prices.json
├── options_overlay.md             ← human-readable options structures
├── options_overlay.json           ← programmatic options data (consumed by HTML report + indexer)
├── per_ticker/
│   └── <T>.md                     ← per-ticker drilldown
├── cells/
│   ├── aggressive/<T>/<T>.state.json
│   └── conservative/<T>/<T>.state.json
└── manifest.json
```

`verdict_ledger.json` is the cross-script contract. Fields consumed downstream:
`ticker`, `name`, `sector_sic`, `market_cap`, `current_price`,
`aggressive_*`, `conservative_*`, `pt_compression_pct`, `classification`,
`aggressive_executive_summary`.

---

## Setup / first-run

```bash
# Python 3.10+ required. The repo's existing venv lives at .venv/
.venv/bin/python -m pip install -e .

# Required env vars (in .env at repo root)
POLYGON_API_KEY=...        # mandatory for screener + options overlay
OPENAI_API_KEY=...         # OR any other supported LLM provider:
ANTHROPIC_API_KEY=...      # (GOOGLE/XAI/DEEPSEEK/DASHSCOPE/ZHIPU/OPENROUTER)

# Tests — see the dedicated "Testing" section for baseline + known flake
.venv/bin/python -m pytest tests/ -x -q
```

`runs/` is fully gitignored (so `runs/index.db` and all per-run artifacts
stay local). `.env` is also gitignored.

---

## Testing

```bash
# Full suite (baseline 2026-07-01 post-cadence-restructure: 1227 pass,
# 2 env-gated skips, ~2min; deselect the polygon_pacer flake under load)
.venv/bin/python -m pytest tests/ -x -q

# Single test file
.venv/bin/python -m pytest tests/test_news_enrichment.py -x -q

# Single test function
.venv/bin/python -m pytest tests/test_polygon_tier.py::test_tier_a_threshold -x -q

# By marker (defined in pyproject.toml)
.venv/bin/python -m pytest -m unit -x -q      # fast isolated tests
.venv/bin/python -m pytest -m smoke -x -q      # quick sanity checks
.venv/bin/python -m pytest -m integration -x -q # needs external services
```

Known flake: `tests/test_polygon_pacer.py::test_retry_after_seconds_is_honored`
is timing-sensitive — deselect if a parallel run perturbs sleep budgets.

---

## Core package architecture (`tradingagents/`)

The framework is a LangGraph state machine. `TradingAgentsGraph.propagate(ticker, date)`
is the entry point — it builds the graph, creates initial `AgentState`, and
runs it to a terminal node that returns a portfolio decision.

```
tradingagents/
├── graph/                        ← LangGraph wiring
│   ├── trading_graph.py          ← TradingAgentsGraph (main class, builds the graph)
│   ├── setup.py                  ← GraphSetup: node/edge registration
│   ├── propagation.py            ← Propagator: initial state + graph invoke
│   ├── conditional_logic.py      ← Edge predicates (debate rounds, risk discussion)
│   ├── signal_processing.py      ← Post-run signal extraction
│   ├── reflection.py             ← Cross-run memory reflection (decision log)
│   └── checkpointer.py           ← LangGraph checkpoint resume (opt-in)
│
├── agents/                       ← Node implementations (each is a LangGraph node)
│   ├── analysts/                 ← Four analyst nodes (fundamentals, market, news, social_media)
│   ├── researchers/              ← Bull/bear researchers (structured debate)
│   ├── managers/                 ← Research manager + portfolio manager (structured output)
│   ├── trader/                   ← Trader node (structured output: Buy/Sell/Hold)
│   ├── risk_mgmt/                ← Risk discussion nodes
│   ├── schemas.py                ← Pydantic schemas for structured-output agents
│   └── utils/
│       ├── agent_states.py       ← AgentState, InvestDebateState, RiskDebateState TypedDicts
│       ├── agent_utils.py        ← Abstract tool functions (get_stock_data, get_news, etc.)
│       └── memory.py             ← TradingMemoryLog (decision log persistence)
│
├── dataflows/                    ← Data vendor abstraction layer
│   ├── interface.py              ← Vendor-agnostic tool interface
│   ├── config.py                 ← set_config() wires DEFAULT_CONFIG → vendor routing
│   ├── polygon_*.py              ← Polygon.io implementations
│   ├── alpha_vantage*.py         ← Alpha Vantage implementations
│   ├── y_finance.py              ← yfinance implementations
│   └── news_enrichment_loader.py ← Loads pre-computed news enrichment from env var
│
├── llm_clients/                  ← Provider abstraction
│   ├── factory.py                ← create_llm_client(provider, model) → LangChain LLM
│   ├── model_catalog.py          ← Canonical model names per provider
│   ├── openai_client.py, anthropic_client.py, google_client.py, azure_client.py
│   └── base_client.py            ← Base class for provider clients
│
├── default_config.py             ← DEFAULT_CONFIG dict (LLM, vendors, debate rounds)
├── screener/                     ← Universe screening (technical + fundamental scoring)
└── ui/                           ← Rich terminal UI components

cli/                              ← Interactive CLI (`tradingagents` entry point)
    └── main.py                   ← Typer app; `python -m cli.main` or `tradingagents`
```

**Graph flow**: Analysts → Research Manager → Bull/Bear Debate (N rounds) →
Trader → Risk Discussion (N rounds) → Portfolio Manager → Decision.

**Data vendor routing**: `default_config.py::data_vendors` sets category-level
defaults (polygon, alpha_vantage, yfinance). `tool_vendors` overrides at the
individual tool level (e.g. `get_insider_transactions` routes to
`yfinance,alpha_vantage` because Polygon free tier lacks insider data).
`dataflows/config.py::set_config()` wires this at graph construction time.

**Structured output**: Research Manager, Trader, and Portfolio Manager use
Pydantic schemas (`agents/schemas.py`) with provider-native structured output
(OpenAI json_schema, Gemini response_schema, Anthropic tool-use). A render
helper converts parsed models back to markdown for reports and memory log.

---

## Common workflows

### Weekly tick (most common)
```bash
.venv/bin/python scripts/weekly_workflow.py --top 25
```
Phase 3 (DIFF) prints NEW vs REPEAT vs DROPPED against the last matrix run.
Phase 5 prints the exact next-step commands.

### Just refresh options on an existing matrix
```bash
.venv/bin/python scripts/build_options_overlay.py \
    --matrix-run runs/<matrix_id> \
    --strategy-mode long-call \
    --long-call-delta 0.55
```

### Build the cross-run HTML dashboard
```bash
.venv/bin/python scripts/build_html_report.py \
    --runs runs/<matrix_id_1>:large runs/<matrix_id_2>:mid \
    --output runs/cross_run_$(date +%Y-%m-%d)/report.html
```

### Query the catalog
```bash
# Summary
.venv/bin/python scripts/index_runs.py --query

# Or raw SQL
sqlite3 runs/index.db "SELECT ticker, COUNT(*) AS n FROM ticker_history GROUP BY ticker ORDER BY n DESC LIMIT 10"
```

### Force re-index (e.g. after editing an indexer field)
```bash
.venv/bin/python scripts/index_runs.py --force
```

### Portfolio health check (after each weekly matrix tick)
```bash
.venv/bin/python scripts/portfolio_health.py \
    --positions-file runs/portfolio/positions.json \
    --policy-file runs/portfolio/policy.json \
    --output runs/portfolio/snapshots/$(date +%Y-%m-%d).md
```

### Log an executed trade
```bash
.venv/bin/python scripts/portfolio_log_action.py \
    --ticker <T> --action {OPEN|CLOSE|TRIM|ADD|ROLL|EXIT|NOTE} \
    --qty <N> --premium <PER_SHARE> --underlying <PRICE> \
    --strike <K> --expiry <YYYY-MM-DD> --notes "<freeform>"
```

---

## CLI flag reference (all scripts)

`run_screener.py`: `--top`, `--chain-top`, `--chain-runner`, `--target-date`,
`--output-dir`, `--technical-weight`, `--fundamental-weight`,
`--min-mcap`, `--max-mcap`, `--min-dollar-adv`, `--min-price`,
`--universe-limit`, `--min-request-interval`.

`run_copilot_matrix.py`: `--tickers ...` **xor** `--tickers-from-latest-screener`,
`--top N`, `--profiles aggressive conservative`, `--max-parallel`,
`--stop-on-overweight`, `--no-stage-b`, `--date`, `--run-id`,
`--no-dashboard`.

`run_copilot_persona_aligned.py`: `<tickers>...`, `--run-id`, `--date`.

`scripts/build_options_overlay.py`: `--matrix-run`, `--strategy-mode {tier-driven,long-call}`,
`--long-call-delta` (default 0.55), `--min-oi`, `--risk-free`,
`--ticker-limit`, `--snapshot-date`, `--verbose`.

`scripts/score_picks_iv_surface.py`: `--matrix-run` (required, reads
`options_overlay.json`), `--earnings-calendar` (auto-discovers
`<matrix-run>/earnings_calendar.json`), `--snapshot-date`, `--output`,
`--strike-window-pct` (default 25), `--expiry-window-days` (default 60),
`--pace-seconds`. Implements medloh/stockpile's 5-param IV-surface fit and
flags picks with earnings inside the option's expiry window.

`scripts/build_chronos_overlay.py`: `--matrix-run`, `--model` (default
`amazon/chronos-bolt-base` ~200 MB), `--prediction-length` (default 90 trading
days), `--context-length` (default 504 ≈ 2y), `--quantiles` (default
`0.1,0.5,0.9`), `--device {auto,mps,cuda,cpu}`, `--include-vetoed`,
`--snapshot-date`, `--polygon-pace-seconds`. Requires `pip install chronos-forecasting`.

`scripts/build_run_accounting.py`: `--matrix-run`, `--snapshot-date`.

`scripts/build_html_report.py`: `--runs`, `--output`, `--title`, `--subtitle`.

`scripts/index_runs.py`: `--runs-dir`, `--db`, `--force`, `--query`.

`scripts/weekly_workflow.py`: `--top`, `--target-date`, `--use-screener-run`,
`--chain`, `--chain-top`, `--chain-runner` (default `run_copilot_matrix.py`),
`--chain-max-parallel`, `--dry-run`, plus universe-shaping pass-throughs:
`--min-mcap` (default 2B), `--max-mcap` (default 10B = mid-cap focus),
`--min-dollar-adv` (default 50M), `--min-price` (default 5.0),
`--universe-limit`, `--min-request-interval`.

`scripts/portfolio_load_context.py`: `--positions-file`, `--policy-file`,
`--matrix-run` (auto-discovered if omitted), `--trades-log`, `--output`,
`--validate`.

`scripts/portfolio_check.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--trades-log`, `--ticker <T>` (limit to one position),
`--json`.

`scripts/portfolio_allocation.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--candidate <T>` (simulator mode: emits BUY ticket with
starter sizing from policy), `--json`.

`scripts/portfolio_trade_tickets.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--trades-log`, `--max-new-picks` (default 5),
`--json`.

`scripts/portfolio_health.py`: `--positions-file`, `--policy-file`,
`--matrix-run`, `--trades-log`, `--output` (write markdown to file).

`scripts/portfolio_log_action.py`: `--ticker`, `--action`, `--id`, `--qty`,
`--premium`, `--underlying`, `--strike`, `--expiry`, `--notes`, `--source`,
`--ts`, `--log-file`.

`scripts/backtest_signal_report.py`: `--backtest` (required, realized-backtest
JSON), `--db` (default `runs/index.db`, opened read-only), `--output`
(markdown, default `backtest_signal_report.md` next to the backtest JSON),
`--json-output` (default same stem `.json`).

`scripts/build_friday_packet.py`: `--date` (default today), `--runs-dir`,
`--positions-file`, `--policy-file`, `--skip-live` (no network; gap column
shows "—").

`scripts/approve_lean_signal.py`: `--id` (repeatable), `--unapprove`,
`--list`, `--signals-file`, `--approvals-log`.

`scripts/friday_options_refresh.py`: no required args; discovers today's
matrix runs by ledger date.

`scripts/export_lean_signals.py`: `--matrix-run` (repeatable), `--output`,
`--strict` (hard-fail on mixed snapshot dates; default warns and proceeds).

`run_copilot_persona_aligned.py` (updated): `<tickers>...`, `--run-id`,
`--date`, `--risk-profile`, `--persona-routing {persona-aligned,aggressive-aligned}`,
`--news-enrichment`.

`scripts/weekly_workflow.py` (updated): earnings enrichment is **default ON**;
opt out with `--no-enrich-earnings`. `--enrich-news` remains opt-in.

---

## Support scripts (not part of the cadence)

These exist in `scripts/` but aren't called automatically by
`weekly_workflow.py`. Use the right one for the right job; don't wire them
into the weekly tick without explicit confirmation.

| Script | Purpose |
|---|---|
| `build_earnings_calendar.py` | Builds `earnings_calendar.json` next to a screener or matrix run (auto-discovered by `score_picks_iv_surface.py` for its earnings-inside-expiry flag). Tickers from `--screener-run`, `--matrix-run`, or explicit `--tickers`. |
| `build_short_interest.py` | Polygon biweekly short-interest + daily short-volume snapshot. Wired into the portfolio skill to surface squeeze potential. `--tickers` or `--from-positions`. |
| `build_theme_momentum.py` | 5d/20d/60d returns for a basket + relative strength vs SPY. Answers "is the AI stack still ripping?" `--tickers` or `--from-positions`. |
| `build_macro_snapshot.py` | VIX / SPX 5d / 10y-3m yield curve → `normal` / `defensive` / `halt` regime. **Advisory by default**; the weekly workflow chooses via `--macro-gate` whether non-normal warns / throttles / blocks. |
| `build_position_greeks.py` | Live Δ/Γ/Θ/Vega per options position from Polygon `/v3/snapshot/options/`, plus portfolio-level aggregates. Consumed by the portfolio skill. |
| `compare_insider_ablation.py` | Markdown comparison of verdict ledgers across the broken / ablated / fixed insider-tool trio — the empirical basis for the `VETO=0, —=1, C=2, B=3, A=4` tier rank ladder used by the portfolio advisor. |
| `grounding_audit.py` | Deterministic per-ticker grounding-risk score over a completed matrix run; flags fabrication risk in researcher / risk-debate / PM nodes that lack grounding tools. |
| `run_weekly_all_tiers.py` | Runs `weekly_workflow.py` sequentially across mid + large + mega tiers (Polygon free-tier is 5 req/min, so serializing is forced anyway). Default is screen-only — pass `--chain` to also matrix-run each tier's NEW list. |
| `watch_pipeline.py` | Live TUI dashboard over a weekly / matrix log file (`runs/weekly_workflow_*.log`, `runs/matrix_<id>_*.log`, launchd's `runs/weekly_workflow.log`). Read-only. |
| `smoke_structured_output.py` | End-to-end smoke for the three structured-output agents (Research Manager, Trader, Portfolio Manager) against a real LLM provider. Use to verify `json_schema` / `response_schema` / tool-use bindings before bumping a model. |
| `backtest_signal_report.py` | Joins a realized-backtest JSON to `runs/index.db` and emits signal-vs-outcome correlations + bucket cross-tabs (cap tier, tenor, Chronos quantile, recurrence, score). Source of the "Empirical signal findings" section above — re-run after every backtest cycle. Read-only on the db. |
| `backtest_picks.py` | **The realized backtest.** Queries `index.db` for historical PICKs + option legs, fetches Polygon daily bars (underlying + OCC option tickers via `dataflows/polygon_bars.py`), computes realized hold ROI / PT-hit per pick. Produced `runs/backtest_<date>.json`. |
| `backtest_exit_rules.py` | Exit-discipline simulation on the same picks (hold vs exit-at-cons vs exit-at-aggr vs half). Produced `runs/backtest_exits_<date>.json` — the empirical basis for the LEAN exit rules. |
| `build_friday_packet.py` | **Friday decision packet** — one-page md+html in `runs/friday_packet_<date>/`: go/no-go banner, ranked tickets (B→A→C) with OCC contract / limit / qty / exits, live-vs-anchor gap check, freshness footer, approval checklist. Reads all matrix runs sharing the trade date (content-based, not mtime). |
| `friday_options_refresh.py` | Same-day 14:00 refresh: discovers today's matrix runs by ledger date, re-runs the options overlay on each, rebuilds the packet. Never touches `lean/signals.json`. Driven by `com.tradingagents.friday-refresh.plist`. |
| `approve_lean_signal.py` | The only sanctioned way to flip `approved` in `lean/signals.json`. `--list`, `--id <TICKER-DATE>` (repeatable), `--unapprove`; audit-logs to `lean/approvals_log.jsonl`. |
| `export_lean_signals.py` | Flattens approved matrix picks into `lean/signals.json` for the LEAN execution integration (see the **LEAN execution integration** section). Pure file transform, no network. |

---

## Live market data — use the Massive MCP, not guesses

For **live** prices, quotes, options chains, and greeks, route through the
Massive MCP (`mcp__massive__*`) — `search_endpoints` → `call_api`, with
`store_as` + `query_data` for multi-step joins. Don't quote prices,
deltas, or IV from memory; the user has explicitly flagged this as a
common failure mode (memory: `feedback_massive_mcp_usage.md`).

The `scripts/build_*` family stays the right path for **batch** lookups
feeding a run artifact — don't replace those with the MCP. Rough split:

- **Single-ticker, ad-hoc question** ("what's NVDA's 0.55Δ Jan strike right
  now?") → Massive MCP.
- **Run-time artifact** (matrix overlay, portfolio greeks, weekly tick) →
  the existing scripts.
- **Library docs / SDK syntax** ("what's the Polygon snapshot endpoint
  shape?") → Context7 MCP, not web search.

---

## Don'ts

- ❌ Don't add new dependencies casually — `requirements.txt` is intentionally
  minimal (`.` for editable). Heavy deps go in `pyproject.toml::dependencies`.
- ❌ Don't commit anything under `runs/` — it's all gitignored for a reason.
- ❌ Don't write to `runs/index.db` from anywhere except `scripts/index_runs.py`.
- ❌ Don't add `--parallel` thinking it's an alias for `--max-parallel`. It isn't.
- ❌ Don't drop the `stdin=subprocess.DEVNULL` from any subprocess invocation.
- ❌ Don't rebuild the cadence around an arbitrary daily/intraday loop. The
  user explicitly chose weekly + on-demand. If you think a different cadence
  is better, raise it as a question, don't unilaterally implement it.
- ❌ Don't auto-edit `runs/portfolio/positions.json` from any portfolio
  script. `portfolio_log_action.py` appends to the log; reconciling the log
  to positions is the user's job. The two are designed to diverge until
  reconciled — that divergence is signal the skill surfaces.
- ❌ Don't mix per-share and per-contract option fields. `premium_paid_per_share`
  is the broker-quoted per-share price; `qty × 100 × per_share = total cost`.
  See `runs/portfolio/positions.schema.md` (generated on first portfolio-skill
  run; `runs/` is gitignored, so a fresh checkout won't have it until then).
- ❌ Don't quote live prices, greeks, or IV from memory. Route ad-hoc
  market-data lookups through the Massive MCP (`mcp__massive__*`). The
  `scripts/build_*` family stays the right tool for batch artifacts.
- ❌ Don't size positions by modeled upside or rank entries by screener
  composite score. Both have ~zero (or mildly negative) correlation with
  realized ROI — see "Empirical signal findings". Upside magnitude is
  narrative, not signal; the composite score is a candidate filter only.
- ❌ Don't let the LEAN integration bypass the human approval gate or the weekly
  cadence — `lean/signals.json` rows default `approved:false`; only the user
  flips them. Keep the exit-rule names/semantics in sync across
  `scripts/export_lean_signals.py`, `lean/algorithm/`, and `lean/signals.schema.md`.

---

## Persisted session notes

For GitHub Copilot CLI:
`~/.copilot/session-state/<id>/files/codebase_conventions.md` carries
session-survival-grade notes. A shell wrapper at `~/.zshrc` routes the
memory store to the correct identity for `justinpreston/*` repos.

For Claude Code: project memory lives at
`~/.claude/projects/-Users-jpp5q-Documents-GitHub-TradingAgents/memory/`.
