"""Technical-filter signals for the early-cycle screener.

We're looking for the *early* phase of a parabolic move — the period after
a multi-month base, where:

  * 50-day MA has crossed above the 200-day MA (or is about to)
  * RSI(14) is in the 50–70 range — momentum is positive but not yet euphoric
    (avoid late entries where RSI > 78)
  * 20-day average dollar volume has expanded vs the 60-day baseline
    (institutions are accumulating)
  * Price is breaking out from a 6+ month consolidation base
  * Distance from 252-day high is small (within 10% — not 30%+)

Each signal contributes to a 0–100 ``technical_score``. Names that score
above ~55 are worth deeper analysis; below ~40 are screened out.

We pull ~270 trading days per ticker from
``/v2/aggs/ticker/{T}/range/1/day/...`` — one call per ticker, sequential.
At Polygon's standard rate limits this runs ~5–10ms per call after warmup,
so ~1500 tickers takes 5–10 minutes.

Caching: by default :func:`compute_technical_signals` consults a disk
cache keyed by ``(ticker, end_date)``. Because the end_date is part of
the key, the cache is naturally self-invalidating — each weekly run uses
a new end_date and refetches; mid-week re-runs against the same end_date
are served instantly from cache. Pass ``use_cache=False`` to bypass.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import date, timedelta
from typing import Sequence

from tradingagents.dataflows._disk_cache import DiskCache
from tradingagents.dataflows.polygon_common import _make_request

log = logging.getLogger(__name__)

# No TTL — the cache key includes end_date so entries are date-stamped and
# never serve stale data. Old entries accumulate; clean up via DiskCache.clear()
# if disk usage becomes a concern (~1 KB per ticker-day).
_TECHNICALS_CACHE = DiskCache("technicals")


@dataclass
class TechnicalSignals:
    ticker: str
    bars_count: int = 0

    last_close: float = 0.0
    ma_50: float = 0.0
    ma_200: float = 0.0
    rsi_14: float = 0.0

    high_252: float = 0.0
    low_252: float = 0.0
    pct_off_high: float = 0.0  # negative number; -0.05 = 5% below 52w high
    pct_above_low: float = 0.0  # positive number; +1.50 = 150% above 52w low

    adv_20: float = 0.0
    adv_60: float = 0.0
    vol_expansion: float = 0.0  # adv_20 / adv_60 ratio

    # Stack flags
    ma_stack_bullish: bool = False  # 50d > 200d
    ma_stack_recent: bool = False  # 50d crossed 200d in last 90d
    rsi_sweet_spot: bool = False  # RSI in [50, 70]
    rsi_overheated: bool = False  # RSI > 78
    base_breakout: bool = False  # near 252d high after 120+ day consolidation
    base_consolidation_days: int = 0  # days since last 252d high reset

    technical_score: float = 0.0
    flags: list[str] = field(default_factory=list)


def _moving_average(closes: Sequence[float], window: int) -> float:
    if len(closes) < window:
        return 0.0
    return sum(closes[-window:]) / window


def _rsi(closes: Sequence[float], period: int = 14) -> float:
    """Wilder's RSI(14). Returns 0 if insufficient data."""
    if len(closes) < period + 1:
        return 0.0
    gains = 0.0
    losses = 0.0
    # Seed
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    # Smooth
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def fetch_daily_bars(
    ticker: str,
    *,
    end_date: date | None = None,
    lookback_days: int = 380,
) -> list[dict]:
    """Pull ~lookback_days trading days of split-adjusted daily bars.

    Calendar days are inflated to ~380 to ensure we get >= 252 trading days
    even with weekends + holidays. Polygon limits: 5000 bars/call max.
    """
    end_d = end_date or date.today()
    start_d = end_d - timedelta(days=lookback_days)
    payload = _make_request(
        f"/v2/aggs/ticker/{ticker}/range/1/day/{start_d.isoformat()}/{end_d.isoformat()}",
        params={"adjusted": "true", "sort": "asc", "limit": 5000},
    )
    return payload.get("results") or []


def _signals_from_dict(d: dict) -> "TechnicalSignals":
    """Reconstruct :class:`TechnicalSignals` from a JSON-cached payload.

    Tolerant of schema additions: unknown keys ignored, missing keys default.
    """
    valid = {f.name for f in fields(TechnicalSignals)}
    return TechnicalSignals(**{k: v for k, v in d.items() if k in valid})


def compute_technical_signals(
    ticker: str,
    *,
    end_date: date | None = None,
    bars: list[dict] | None = None,
    use_cache: bool = True,
) -> TechnicalSignals:
    """Compute all technical signals + a composite score in [0, 100].

    Pass ``bars`` if you already have them (to avoid duplicate calls);
    otherwise this fetches ~380 calendar days of daily bars.

    By default a disk cache keyed by ``(ticker, end_date)`` is consulted.
    Set ``use_cache=False`` to bypass. Caller-supplied ``bars=`` always
    bypasses both fetch and cache so test/mock data can't poison the cache.
    """
    end_d = end_date or date.today()
    persist_to_cache = use_cache and bars is None
    cache_key = f"{ticker}_{end_d.isoformat()}"

    if persist_to_cache:
        cached = _TECHNICALS_CACHE.get(cache_key)
        if cached is not None:
            try:
                return _signals_from_dict(cached)
            except (TypeError, KeyError) as e:
                log.debug("technicals cache: malformed entry for %s (%s) — refetching", cache_key, e)

    if bars is None:
        bars = fetch_daily_bars(ticker, end_date=end_d)

    sig = _build_technical_signals(ticker, bars)

    if persist_to_cache:
        try:
            _TECHNICALS_CACHE.set(cache_key, asdict(sig))
        except Exception as e:  # noqa: BLE001
            log.debug("technicals cache: failed to persist %s: %s", cache_key, e)
    return sig


def _build_technical_signals(ticker: str, bars: list[dict]) -> TechnicalSignals:
    """Pure compute path — no I/O, no cache."""
    sig = TechnicalSignals(ticker=ticker, bars_count=len(bars))
    if len(bars) < 200:
        sig.flags.append("insufficient_history")
        return sig

    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]

    sig.last_close = closes[-1]
    sig.ma_50 = _moving_average(closes, 50)
    sig.ma_200 = _moving_average(closes, 200)
    sig.rsi_14 = _rsi(closes, 14)

    window_252 = bars[-252:] if len(bars) >= 252 else bars
    sig.high_252 = max(b["h"] for b in window_252)
    sig.low_252 = min(b["l"] for b in window_252)
    if sig.high_252 > 0:
        sig.pct_off_high = (sig.last_close - sig.high_252) / sig.high_252
    if sig.low_252 > 0:
        sig.pct_above_low = (sig.last_close - sig.low_252) / sig.low_252

    if len(bars) >= 60:
        adv20_bars = bars[-20:]
        adv60_bars = bars[-60:]
        sig.adv_20 = sum(b["c"] * b["v"] for b in adv20_bars) / len(adv20_bars)
        sig.adv_60 = sum(b["c"] * b["v"] for b in adv60_bars) / len(adv60_bars)
        if sig.adv_60 > 0:
            sig.vol_expansion = sig.adv_20 / sig.adv_60

    sig.ma_stack_bullish = sig.ma_50 > sig.ma_200 > 0

    if len(closes) >= 290:
        ma50_now = sig.ma_50
        ma200_now = sig.ma_200
        ma50_90d_ago = _moving_average(closes[:-90], 50)
        ma200_90d_ago = _moving_average(closes[:-90], 200)
        was_below = ma50_90d_ago <= ma200_90d_ago
        is_above = ma50_now > ma200_now
        sig.ma_stack_recent = was_below and is_above

    sig.rsi_sweet_spot = 50.0 <= sig.rsi_14 <= 70.0
    sig.rsi_overheated = sig.rsi_14 > 78.0

    if window_252:
        running_high = 0.0
        days_since_new_high = 0
        max_consolidation = 0
        for b in window_252:
            if b["h"] > running_high:
                running_high = b["h"]
                if days_since_new_high > max_consolidation:
                    max_consolidation = days_since_new_high
                days_since_new_high = 0
            else:
                days_since_new_high += 1
        sig.base_consolidation_days = max_consolidation
        sig.base_breakout = (
            max_consolidation >= 120
            and sig.pct_off_high > -0.05
        )

    score = 0.0
    if sig.ma_stack_bullish:
        score += 20
        sig.flags.append("ma_stack_bullish")
    if sig.ma_stack_recent:
        score += 15
        sig.flags.append("ma_stack_recent")
    if sig.rsi_sweet_spot:
        score += 15
        sig.flags.append("rsi_sweet_spot")
    if sig.rsi_overheated:
        score -= 25
        sig.flags.append("rsi_overheated")
    if sig.vol_expansion >= 1.5:
        score += 15
        sig.flags.append("vol_expansion_strong")
    elif sig.vol_expansion >= 1.2:
        score += 8
        sig.flags.append("vol_expansion_mild")
    if sig.base_breakout:
        score += 25
        sig.flags.append("base_breakout")
    if sig.pct_off_high > -0.10:
        score += 10
        sig.flags.append("near_52w_high")
    elif sig.pct_off_high < -0.30:
        score -= 10
        sig.flags.append("far_below_52w_high")
    if sig.pct_above_low >= 0.50:
        score += 5
        sig.flags.append("uptrending_ytd")

    sig.technical_score = max(0.0, min(100.0, score))
    return sig
