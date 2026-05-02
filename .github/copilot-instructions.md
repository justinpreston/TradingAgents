# GitHub Copilot — TradingAgents instructions

> **Canonical guide: [`/CLAUDE.md`](../CLAUDE.md).** Read it first for the
> full pipeline, command surface, run-output anatomy, and CLI flag reference.
> The critical must-knows are inlined below so you don't have to chase a
> reference to do the right thing on the first attempt.

---

## Critical must-knows (do not violate)

### 1. The cadence is locked-in — don't reinvent it
Weekly Friday screener + on-demand matrix on **NEW tickers only** + same-day
options refresh before entry. Run `scripts/weekly_workflow.py --top 25` and
follow its Phase 5 next-step commands. The Phase 3 SQLite-driven diff is the
whole point — never re-run matrix on REPEAT tickers whose verdicts are still
valid.

### 2. Long calls are the default options strategy
User preference: *"I generally prefer long calls for ease of trading."*
Default is `--strategy-mode long-call --long-call-delta 0.55` (slightly ITM).
Tier-driven (spreads) mode is still available but it is **not** the default.

### 3. Files-first, SQLite-derived storage
- `runs/<id>/*.json` and `*.md` are the **authoritative** source of truth.
  Never modify them programmatically except through their owning script.
- `runs/index.db` is **purely derived** by `scripts/index_runs.py`. Never
  write to it from anywhere else. Never commit it (`runs/` is gitignored).

### 4. Tier A/B/C is derived per-row, NOT a stored field
The `classification` column in `verdict_ledger.json` only takes `PICK` or
`VETOED`. Tier rules:
- `classification != 'PICK'` → no tier
- `conservative_pt is None` → **C** (cons declined to model)
- `pt_compression_pct < 5.0` → **A** (tight dual-frame agreement)
- otherwise → **B** (cons engaged but skeptical)

Two implementations must stay in sync if you change the threshold:
`scripts/build_options_overlay.py::_tier()` and
`scripts/index_runs.py::_classification_to_tier()`.

### 5. Subprocess invariants (each has historically broken matrix runs)
- `stdin=subprocess.DEVNULL` on every child process call (commit `50b41e4`).
- Flag is `--max-parallel`, NOT `--parallel`.
- `--stop-on-overweight 0` for full-coverage matrix runs.
- `load_dotenv('.env')` with explicit path (Python 3.13 quirk).

### 6. Trade recommendations always include current price
Without it, RR/upside numbers are meaningless. Polygon
`/v2/aggs/ticker/{T}/prev` is the canonical source. The `current_prices.json`
file in each matrix run already has them.

---

## Run the pipeline

```bash
# Weekly tick (auto-detects NEW vs REPEAT vs DROPPED, prints next steps)
.venv/bin/python scripts/weekly_workflow.py --top 25

# Same-day options refresh before entry
.venv/bin/python scripts/build_options_overlay.py \
    --matrix-run runs/<matrix_id> --strategy-mode long-call

# Tests baseline (267 pass, 41 subtests, ~4s)
.venv/bin/python -m pytest tests/ -x -q
```

Full command reference, override triggers, run-output structure, and CLI
flag reference: see [`/CLAUDE.md`](../CLAUDE.md).

---

## Required env vars (in `.env` at repo root)

```
POLYGON_API_KEY=...   # mandatory for screener + options overlay
OPENAI_API_KEY=...    # OR any other supported LLM provider key
                      # (ANTHROPIC/GOOGLE/XAI/DEEPSEEK/DASHSCOPE/ZHIPU/OPENROUTER)
```

`.env` and `runs/` are both gitignored. Do not commit either.

---

## Don'ts

- ❌ Don't add `--parallel` thinking it's an alias for `--max-parallel`. It isn't.
- ❌ Don't write to `runs/index.db` from anywhere except `scripts/index_runs.py`.
- ❌ Don't rebuild the cadence around a daily/intraday loop without explicit user confirmation.
- ❌ Don't drop `stdin=subprocess.DEVNULL` from subprocess invocations.
- ❌ Don't commit anything under `runs/` — it's all gitignored.
- ❌ Don't add new dependencies casually — `requirements.txt` is intentionally minimal.
