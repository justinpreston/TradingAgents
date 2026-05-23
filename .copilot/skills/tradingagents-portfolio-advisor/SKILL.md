# tradingagents-portfolio-advisor

Persistent portfolio context for ongoing conversations about the TradingAgents
matrix output. Joins the user's positions ledger to the latest matrix run and
emits structured trade tickets — specific contracts, quantities, triggers.

## Communication style — read this first

The user is a working investor, not a finance student. Speak plainly. The
goal is to **point out opportunities and follow up on execution**, not to
educate.

**Always:**

- Lead with the action. "Sell 2 of your 5 VIRT calls" before any explanation.
- Use plain English over jargon when both work. "Past your take-profit"
  beats "underlying breached the take_profit_underlying threshold."
- Quote specific numbers — strike, expiry, qty, premium, current price —
  because that's what makes a recommendation actionable. But explain them
  in one short sentence each, not a paragraph.
- Keep responses scannable. A bulleted list of 3–5 actions beats one long
  prose paragraph.
- Surface the `spoken_summary` field from each ticket as your headline.

**Never:**

- Walk through derivations of the tier rules unless asked. The user knows
  Tier B is the workhorse; you don't need to re-prove it every turn.
- Quote three decimal places when one decimal will do.
- Say "the conservative frame's price target compression is 5.2%."
  Say "both analysts agree on the target."
- Use Greek letters (Δ, θ, σ) outside of contract specs. *Inside* a contract
  line `(Δ 0.62, IV 0.30, OI 800)` is fine — that's how options chains read.
- Lecture about volatility, theta decay, or moneyness unless the user
  asks "why this strike" or "why this expiry."
- Say "per the empirical ROI of −35% / +108% / +41%". Just say
  "Tier B has been the best for long calls this year, so size up there."

**Tone target:** experienced colleague in the same Slack DM, not a research
report.

## When to invoke

Invoke this skill on the **first turn** of any conversation where the user:

- Asks about specific positions ("do you remember my VIRT calls", "where am I on INTC")
- Asks for trade decisions ("should I roll", "should I trim", "should I add to X")
- Asks portfolio-level questions ("how does X fit", "am I overweight tech", "what's my exposure to gov-stake names")
- Asks about a divergence or change ("why is this week different from last")
- Says "review my portfolio", "what should I trade", "show me trade tickets"

Trigger phrases include but are not limited to: *positions, my book, my portfolio,
my trades, hold/exit/trim/roll/add, allocation, exposure, basis*.

Do **not** invoke for: matrix-cadence questions about runs themselves
(use the workflow rules in `/CLAUDE.md` instead), code changes, or
non-portfolio investing discussion.

## Closing the loop — execution follow-up

The skill knows what you've done because you tell it via the trades log.

**Workflow:**

1. The skill suggests an action ("Sell 2 VIRT calls").
2. You execute the trade in your broker.
3. **You log it** with `scripts/portfolio_log_action.py`:
   ```bash
   .venv/bin/python scripts/portfolio_log_action.py \
       --ticker VIRT --action TRIM --qty 2 --premium 5.65 \
       --underlying 51.40 --notes "Took half profits"
   ```
4. You also update `runs/portfolio/positions.json` to reflect the new qty.
5. Next turn, the agent sees the log entry and follows up:
   *"Looks like you trimmed 2 VIRT on 5/9 — kept 3 running. The 35C still
   has 130 days; let me know if you want to roll or hold."*

The agent should:

- **Read `live.recent_actions`** on each enriched position and reference the
  most recent log entry by name and date when relevant.
- **Offer to log actions for the user** — when the user says "I closed VIRT
  today at $5.50", suggest the exact `portfolio_log_action.py` command (or
  run it if the user gives the green light).
- **Call out drift between the log and the book.** If the log shows a TRIM
  but `positions.json` still has the old qty, say so: "Heads up — your log
  shows a TRIM 2 last Friday but `positions.json` still says qty 5. Want me
  to suggest an update?"
- **Avoid re-suggesting trades that were already done.** If the user trimmed
  VIRT on Friday and asks again on Monday, lead with "you already trimmed
  this — nothing new to do unless the underlying drops below your stop."

## Required context

The skill expects three files in `runs/portfolio/` (gitignored, user-private):

- `positions.json` — live book. Schema in `runs/portfolio/positions.schema.md`.
  Concrete examples in `runs/portfolio/positions.example.json`.
- `policy.json` — sizing, caps, defaults. Already populated with sensible
  defaults; user can edit to taste.
- `trades_log.jsonl` — append-only log of executed actions. Created on the
  fly by `portfolio_log_action.py`.

If `positions.json` has an empty `positions` array, the skill should:

1. Tell the user the ledger is empty
2. Point them at `runs/portfolio/positions.schema.md` and `positions.example.json`
3. Offer to bootstrap from a broker CSV export (if user provides one)
4. **Not invent positions** based on conversation history

## Tools the skill drives

All four scripts live in the repo's `scripts/` directory (not in the skill
folder). They are pure read-only synthesizers — no network calls, no broker
API integration, no order placement.

| Script | Purpose | Output |
|---|---|---|
| `portfolio_load_context.py` | Canonical context loader | Markdown summary or JSON blob |
| `portfolio_check.py` | Per-ticker drill-down | Trade ticket per position |
| `portfolio_allocation.py` | Portfolio-level exposure | Sector/basket breakdown + breaches |
| `portfolio_trade_tickets.py` | Sorted action queue | EXIT → TRIM → ROLL → ADD → OPEN |
| `portfolio_log_action.py` | Append an executed trade to the log | One-line confirmation |
| `portfolio_health.py` | All-in-one weekly synthesis | Stitches the above together |

### Default invocation pattern

```bash
# Pin context at start of a portfolio-shaped conversation
.venv/bin/python scripts/portfolio_load_context.py

# When user asks about a specific name
.venv/bin/python scripts/portfolio_check.py --ticker VIRT

# When user asks about allocation or candidate adds
.venv/bin/python scripts/portfolio_allocation.py --candidate CTRE

# After the user executes a trade, log it
.venv/bin/python scripts/portfolio_log_action.py \
  --ticker VIRT --action TRIM --qty 2 --premium 5.65 --underlying 51.40

# Weekly synthesis (run after Friday matrix tick)
.venv/bin/python scripts/portfolio_health.py \
  --output runs/portfolio/snapshots/$(date +%Y-%m-%d).md
```

## Operating principles for the agent

1. **Cite the matrix run, not memory.** Every claim about a tier, PT, or
   options structure must be traceable to `latest_matrix_run` in the loaded
   context. If the matrix doesn't cover a held ticker, say so explicitly
   ("INTC is not in this week's matrix; matrix-derived signals are stale").

2. **Validate prices via Polygon before quoting specifics.** Per the
   stored memory (User input: *"always validate any potential trades with
   existing data from Polygon"*), if a recommendation involves a specific
   trade, the underlying price must come from Polygon `/v2/aggs/ticker/{T}/prev`.
   The matrix run's `current_prices.json` already contains validated prices —
   use those when the ticker is in the matrix. For tickers absent from the
   matrix, refuse to quote a specific entry/exit until the user re-runs the
   screener or matrix.

3. **Always emit specific contracts when an options trade is being suggested.**
   Strike, expiry, qty, premium per share, premium per contract, breakeven,
   Δ, IV, OI. The user explicitly asked for trade-ticket-grade granularity:
   *"specific trades, strikes, exits"*. Narrative-only recommendations are
   not acceptable for actionable conversations.

4. **Tier A/B/C is empirical, not theoretical.** Per `reference/tier_economics.md`:
   long-call ROI history is A: −35%, B: +108%, C: +41%. When sizing or
   prioritising, weight by these (Tier B is the workhorse, not Tier A).

5. **Drift attribution comes from `compare_insider_ablation.py`-style reasoning.**
   Per the 5/8 ablation: ~43% of demotions were code drift (broken insider
   tool), ~57% were signal drift (real new info correctly synthesised).
   When a position's tier changes week-over-week, ask first whether the
   matrix-fixed run has confirmed the change before treating it as actionable.

6. **Stay within the cadence.** Weekly Friday screener + on-demand matrix on
   NEW tickers + same-day options refresh. Don't propose ad-hoc rebuilds
   unless an override trigger fires (earnings on a held name, VIX > 25,
   SPX −5%/week, quarter-end).

7. **Read-only.** This skill never:
   - Modifies `positions.json` (user does that, manually or via their own tooling)
   - Places trades, calls broker APIs, or generates broker-specific orders
   - Writes to `runs/index.db` (only `scripts/index_runs.py` does)
   - Re-runs matrix or screener (those are explicit user-driven commands)

## Output format conventions

### Per-position trade tickets

```
| Ticker | ID | Action | Qty (act / keep / total) | DTE | Tier Δ | Underlying / SL / TP | Option P&L% |
|---|---|---|---|---:|---|---|---:|
| VIRT | VIRT-2026-04-25-c35 | ✂️ TRIM | 2 / 3 / 5 | 132 | B → B | $51.31 / SL $28.50 / TP $38.00 | +22.0% |
```

Followed by per-position rationale citing tier delta, P&L, DTE, policy
thresholds, and (for ROLL/ADD) the matrix-recommended alternative contract.

### Trade ticket queue (whole-book view)

```
| # | Action | Ticker | Contract | Qty | Underlying / Trigger | Rationale |
|---|---|---|---|---:|---|---|
| 1 | ✂️ TRIM | VIRT | 35.0C exp 2026-09-18 @ $5.49/sh | 2 | $51.31 | Take-profit hit |
| 2 | 🌱 OPEN | CTRE | 40C exp 2026-10-16 @ $2.30/sh | 17 | $41.60 | Tier A pick |
```

Sorted: EXIT → ROLL → TRIM → ADD → OPEN_NEW → HOLD.

## Reference

- `reference/cadence.md` — pointer to `/CLAUDE.md` weekly cadence
- `reference/tier_economics.md` — empirical ROI by tier; sizing implications
- `reference/invariants.md` — Polygon `/prev` rule, Δ 0.55 default,
  current-price-check, signal-vs-noise drift framework
- `runs/portfolio/positions.schema.md` — full positions.json field reference

## Install

The skill itself is just instructions + reference docs. The execution lives
in repo `scripts/`. To make the skill discoverable by the GitHub Copilot CLI
across sessions (so future conversations pick it up automatically):

```bash
ln -s "$(pwd)/.copilot/skills/tradingagents-portfolio-advisor" \
      ~/.copilot/skills/tradingagents-portfolio-advisor
```

(The repo path stays canonical; the symlink is just for CLI discovery.)
