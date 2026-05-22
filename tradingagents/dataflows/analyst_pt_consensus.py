"""Analyst price-target consensus — grounding source for persona PTs.

The persona LLMs (aggressive / conservative) produce `aggressive_pt` and
`conservative_pt` floats that drive Tier A/B/C classification via the
`pt_compression_pct` rule. Those floats are pure model output with no
grounding tool, which means two ungrounded hallucinations can compress
to <5% and produce a false Tier A.

This module fetches sell-side analyst price-target consensus from
yfinance and exposes it as a grounding source for:

  - The grounding audit (`scripts/grounding_audit.py`) which compares
    persona PTs against the street range and flags outliers.
  - Optionally the bull/bear researchers as a tool — but the default
    integration is post-hoc audit, not inline tool injection, to avoid
    biasing the persona toward whatever street happens to be saying.

Fail-soft: every error path returns `None` so a missing or partial
response never blocks a run.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

try:  # yfinance is a transitive dep via stockstats_utils
    import yfinance as yf
except ImportError:  # pragma: no cover - bootstrap safety
    yf = None  # type: ignore[assignment]


_CACHE: dict[str, "AnalystPriceTargets | None"] = {}


@dataclass(frozen=True)
class AnalystPriceTargets:
    """Sell-side price-target consensus snapshot for a ticker.

    All fields are quote-currency floats (or analyst counts). ``None``
    means the field was not present in the upstream response.
    """

    ticker: str
    current: Optional[float]
    high: Optional[float]
    low: Optional[float]
    mean: Optional[float]
    median: Optional[float]
    number_of_analysts: Optional[int]
    source: str  # "yfinance" | "polygon" | "unavailable"

    def as_dict(self) -> dict:
        return asdict(self)

    def is_populated(self) -> bool:
        """Whether at least one price-target field came back."""
        return any(
            v is not None
            for v in (self.high, self.low, self.mean, self.median)
        )


def _coerce_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # yfinance occasionally returns NaN; treat as missing.
    if f != f:  # NaN check without importing math
        return None
    return f


def _coerce_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_analyst_pt_consensus(ticker: str, *, use_cache: bool = True) -> AnalystPriceTargets:
    """Fetch the analyst price-target consensus snapshot for ``ticker``.

    Returns an ``AnalystPriceTargets`` with ``source="unavailable"`` and
    every field None when yfinance can't be reached or the ticker isn't
    covered. Callers should call ``is_populated()`` before treating the
    result as ground truth.
    """
    key = ticker.upper()
    if use_cache and key in _CACHE:
        cached = _CACHE[key]
        if cached is not None:
            return cached

    if yf is None:
        result = AnalystPriceTargets(
            ticker=key,
            current=None,
            high=None,
            low=None,
            mean=None,
            median=None,
            number_of_analysts=None,
            source="unavailable",
        )
        _CACHE[key] = result
        return result

    try:
        tk = yf.Ticker(key)
        # Preferred: the dedicated `analyst_price_targets` API (yfinance
        # ≥0.2.43). Older versions only exposed equivalent fields under
        # `info`, so we fall back to that if the API call returns nothing.
        targets = None
        try:
            targets = tk.analyst_price_targets  # type: ignore[attr-defined]
        except (AttributeError, KeyError):
            targets = None
        except Exception:  # pragma: no cover - yfinance occasionally raises
            targets = None

        if isinstance(targets, dict) and targets:
            result = AnalystPriceTargets(
                ticker=key,
                current=_coerce_float(targets.get("current")),
                high=_coerce_float(targets.get("high")),
                low=_coerce_float(targets.get("low")),
                mean=_coerce_float(targets.get("mean")),
                median=_coerce_float(targets.get("median")),
                number_of_analysts=None,
                source="yfinance",
            )
            _CACHE[key] = result
            return result

        # Fallback path — pull from `info` dict.
        info = {}
        try:
            info = tk.info or {}
        except Exception:  # pragma: no cover
            info = {}

        high = _coerce_float(info.get("targetHighPrice"))
        low = _coerce_float(info.get("targetLowPrice"))
        mean = _coerce_float(info.get("targetMeanPrice"))
        median = _coerce_float(info.get("targetMedianPrice"))
        count = _coerce_int(info.get("numberOfAnalystOpinions"))
        current = _coerce_float(info.get("currentPrice") or info.get("regularMarketPrice"))

        any_present = any(v is not None for v in (high, low, mean, median))
        result = AnalystPriceTargets(
            ticker=key,
            current=current,
            high=high,
            low=low,
            mean=mean,
            median=median,
            number_of_analysts=count,
            source="yfinance" if any_present else "unavailable",
        )
        _CACHE[key] = result
        return result

    except Exception:  # pragma: no cover - all upstream errors are fail-soft
        result = AnalystPriceTargets(
            ticker=key,
            current=None,
            high=None,
            low=None,
            mean=None,
            median=None,
            number_of_analysts=None,
            source="unavailable",
        )
        _CACHE[key] = result
        return result


def reset_cache() -> None:
    """Test helper — clear the module-level cache."""
    _CACHE.clear()
