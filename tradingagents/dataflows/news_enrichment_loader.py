"""Loader for pre-computed news enrichment context (sentiment + themes).

Reads the path from the ``TRADINGAGENTS_NEWS_ENRICHMENT_PATH`` env var.
When set, agents that handle news (currently ``news_analyst``) prepend a
brief pre-computed summary to their system message so they start with
verified ground truth (FinBERT polarity, theme tags) before issuing
their own tool calls.

The env-var integration was chosen over an ``AgentState`` field so:
- The langgraph checkpointer schema is unchanged.
- Per-cell subprocesses (run_copilot_persona_aligned.py invoked by
  run_copilot_matrix.py) can opt in by exporting one variable, no code
  changes per cell.
- A bare run with no enrichment behaves exactly as before — the loader
  returns ``""`` and the system message is unchanged.

Module-level cache: the JSON is parsed once per process. Matrix cells
each spawn their own subprocess, so per-cell freshness is implicit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENRICHMENT_ENV_VAR = "TRADINGAGENTS_NEWS_ENRICHMENT_PATH"

_CACHE: dict[str, dict[str, Any]] | None = None
_CACHE_KEY: str | None = None


def _load_enrichment_map() -> dict[str, dict[str, Any]]:
    """Load and cache the enrichment file pointed to by the env var.

    Returns an empty dict if the env var is unset or the file is
    malformed / missing. Never raises — enrichment is opt-in and a bad
    file should not break a matrix run.
    """
    global _CACHE, _CACHE_KEY
    path = os.environ.get(ENRICHMENT_ENV_VAR, "").strip()
    if not path:
        _CACHE, _CACHE_KEY = {}, None
        return _CACHE
    if _CACHE_KEY == path and _CACHE is not None:
        return _CACHE
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        _CACHE, _CACHE_KEY = {}, path
        return _CACHE
    _CACHE = {row["ticker"].upper(): row for row in data.get("tickers", [])}
    _CACHE_KEY = path
    return _CACHE


def get_enrichment_for(ticker: str) -> dict[str, Any] | None:
    """Return the enrichment row for ``ticker`` (or None if not present)."""
    if not ticker:
        return None
    return _load_enrichment_map().get(ticker.upper())


def build_enrichment_prefix(ticker: str) -> str:
    """Build a compact pre-computed-news prefix for the news analyst.

    Returns ``""`` (empty string) when no enrichment is available, so
    the caller can unconditionally prepend this to the system message.
    Returns a single paragraph + bullet list when enrichment is present:

        Pre-computed news context for AVGO (last 30d):
          - FinBERT sentiment +0.12 from 68 headlines (range [-1, 1])
          - Top themes: earnings (45%) · product_launch (20%) · analyst_action (15%)
          - Notable triggers: warn (1)

    The analyst should treat this as a hint, NOT as a substitute for its
    own tool calls — the prefix says so explicitly.
    """
    row = get_enrichment_for(ticker)
    if not row:
        return ""

    sentiment = (row.get("sentiment") or {})
    chosen = sentiment.get("finbert") or sentiment.get("keyword")
    if not chosen or not chosen.get("n_headlines"):
        return ""

    bits = [f"Pre-computed news context for {ticker.upper()} "
            f"(last {row.get('lookback_days', '?')}d):"]
    scorer = chosen.get("scorer", "?")
    agg = chosen.get("aggregate", 0.0) or 0.0
    n = chosen.get("n_headlines", 0)
    bits.append(
        f"  - {scorer} sentiment {agg:+.2f} from n={n} headlines "
        f"(range [-1, 1])"
    )

    themes = row.get("themes") or []
    if themes:
        theme_strs = [
            f"{t['label']} ({(t.get('confidence', 0.0) or 0.0):.0%})"
            for t in themes[:3]
        ]
        bits.append(f"  - Top themes: {' · '.join(theme_strs)}")

    triggers = chosen.get("trigger_terms") or []
    if triggers:
        bits.append(f"  - Notable triggers: {', '.join(triggers[:3])}")

    bits.append(
        "  This is a pre-computed signal — verify with your own tool "
        "calls before drawing conclusions, but use it as a tilt prior."
    )
    bits.append("")
    return "\n".join(bits) + "\n"


def reset_cache() -> None:
    """Clear the module-level cache (useful for tests)."""
    global _CACHE, _CACHE_KEY
    _CACHE, _CACHE_KEY = None, None
