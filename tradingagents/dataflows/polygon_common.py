"""Shared utilities for Polygon REST API access.

Polygon is the preferred provider for point-in-time fundamentals (as-reported
SEC filings filtered by ``filing_date.lt``) and split-adjusted historical bars.
This module hosts the request helper, key resolution, in-process caching,
rate-limit handling, and the shared error classes consumed by callers and the
vendor router in :mod:`tradingagents.dataflows.interface`.

Rate limiting
-------------
``_make_request`` supports two layers of throttling:

  * **Reactive** — on HTTP 429/5xx, retries with exponential backoff and
    honors the ``Retry-After`` response header (delta-seconds or HTTP-date).
  * **Proactive** — an opt-in process-wide minimum-interval pacer. Callers
    that issue sustained bursts (e.g. the screener iterating 1000+ tickers)
    enable it via :func:`set_min_request_interval` or the
    :class:`min_request_interval` context manager. Default is 0 (no pacing),
    which preserves the per-call cadence used by the agents pipeline.
"""

from __future__ import annotations

import os
import random
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

import requests

API_BASE_URL = "https://api.polygon.io"

_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
_DEFAULT_TIMEOUT = 30
_MAX_ATTEMPTS = 6
_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_RETRY_AFTER_CAP_S = 120.0


class PolygonError(Exception):
    """Base error class for Polygon REST failures."""


class PolygonRateLimitError(PolygonError):
    """Raised when Polygon returns 429 or signals rate limit exhaustion.

    Mirrors :class:`AlphaVantageRateLimitError` so the vendor router can
    treat it as a fallback trigger.
    """


class PolygonAuthError(PolygonError):
    """Raised when Polygon returns 401/403 — usually a bad/expired API key."""


class PolygonNotFoundError(PolygonError):
    """Raised when the requested resource (ticker, page) is missing."""


def get_api_key() -> str:
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        raise PolygonError(
            "POLYGON_API_KEY environment variable is not set. "
            "Add it to .env or export it before invoking the agent pipeline."
        )
    return api_key


_session_lock = threading.Lock()
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "trading_agents/polygon-client",
                "Accept": "application/json",
            })
            _session = session
        return _session


def _sleep_backoff(attempt: int) -> None:
    base = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
    time.sleep(base + random.random())


_pace_lock = threading.Lock()
_min_interval_s: float = 0.0
_last_request_time: float = 0.0
_pacer_adaptive: bool = False
_PACER_MAX_INTERVAL_S: float = 2.0
_PACER_ADAPT_FACTOR: float = 1.5
_PACER_ADAPT_FLOOR_S: float = 0.05


# Recommended min-request-interval defaults per Polygon plan tier.
#
# Free tier hard-caps at 5 req/min → starting at 2.0s gives the adaptive
# pacer no headroom to back off further; we start at 12.0s (= 5/min ceiling)
# and let adaptive bring it down on success. In practice 0.25s + adaptive
# ramp-on-429 has been the working compromise for free tier — it bursts
# fast at first, then settles into the 1.5–2.0s zone after a few 429s.
#
# Paid tiers ("Stocks Starter" $29/mo and up) are effectively unlimited at
# screener volumes (1500-2000 tickers × few calls each per run). 0.05s
# (= 1200 req/min) gives plenty of headroom against any per-second cap and
# avoids pathological burst behavior.
_TIER_INTERVALS: dict[str, float] = {
    "free":      0.25,   # adaptive ramp will lift this on 429s
    "basic":     0.05,   # Stocks Starter — unlimited
    "starter":   0.05,   # alias
    "developer": 0.05,   # also unlimited
    "advanced":  0.05,
}


def recommended_min_interval_for_tier(tier: str | None = None) -> float:
    """Return the recommended min-interval (seconds) for a Polygon plan tier.

    If ``tier`` is None, reads ``POLYGON_TIER`` env var. An empty string is
    treated as an explicit (unknown) tier and falls back to free-tier
    interval — the safe choice. Set ``POLYGON_MIN_REQUEST_INTERVAL_S`` to
    override entirely (always wins regardless of tier).

    The returned value is the *initial* interval. With ``adaptive=True``
    in :func:`set_min_request_interval`, the pacer will ratchet up on
    429s — so even an aggressive initial value self-corrects in a single
    run.
    """
    explicit = os.getenv("POLYGON_MIN_REQUEST_INTERVAL_S")
    if explicit:
        try:
            return max(0.0, float(explicit))
        except ValueError:
            pass

    if tier is None:
        tier = os.getenv("POLYGON_TIER", "")
    name = tier.strip().lower()
    return _TIER_INTERVALS.get(name, _TIER_INTERVALS["free"])



def set_min_request_interval(seconds: float, *, adaptive: bool = False) -> None:
    """Set the global minimum interval (seconds) between Polygon REST calls.

    Pass 0 to disable pacing. Process-wide; intended for callers that issue
    sustained bursts (e.g. the screener). The agents pipeline leaves this at
    0 — its per-ticker cadence does not benefit from artificial spacing.

    When ``adaptive=True``, the pacer ratchets the interval up by
    :data:`_PACER_ADAPT_FACTOR` each time a 429 is observed, capped at
    :data:`_PACER_MAX_INTERVAL_S`. Combined with the
    :class:`min_request_interval` context manager this self-tunes within a
    single run and resets cleanly on exit.
    """
    global _min_interval_s, _pacer_adaptive
    with _pace_lock:
        _min_interval_s = max(0.0, float(seconds))
        _pacer_adaptive = bool(adaptive)


class min_request_interval:
    """Context manager wrapper around :func:`set_min_request_interval`.

    Restores the prior interval *and* adaptive-mode flag on exit, so nested
    or interleaved scopes do not permanently alter the process. Not
    refcounted: concurrent scopes on different threads can interleave their
    save/restore non-deterministically; intended for single-threaded
    orchestration.
    """

    def __init__(self, seconds: float, *, adaptive: bool = False) -> None:
        self.seconds = seconds
        self.adaptive = adaptive
        self._prev_interval: float | None = None
        self._prev_adaptive: bool | None = None

    def __enter__(self) -> "min_request_interval":
        global _min_interval_s, _pacer_adaptive
        with _pace_lock:
            self._prev_interval = _min_interval_s
            self._prev_adaptive = _pacer_adaptive
            _min_interval_s = max(0.0, float(self.seconds))
            _pacer_adaptive = bool(self.adaptive)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        global _min_interval_s, _pacer_adaptive
        with _pace_lock:
            if self._prev_interval is not None:
                _min_interval_s = self._prev_interval
            if self._prev_adaptive is not None:
                _pacer_adaptive = self._prev_adaptive
        return False


def _wait_for_slot() -> None:
    """Block (if pacing is enabled) until the next allowed request slot.

    Uses a reserve-then-sleep pattern: the next slot timestamp is computed
    and committed to ``_last_request_time`` under the lock, and the actual
    sleep happens outside the lock so concurrent callers can sleep in
    parallel while still serialising their slot reservations.
    """
    global _last_request_time
    with _pace_lock:
        now = time.monotonic()
        if _min_interval_s <= 0:
            _last_request_time = now
            return
        slot = max(now, _last_request_time + _min_interval_s)
        _last_request_time = slot
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _adapt_on_rate_limit() -> float | None:
    """Ratchet the pacer up on 429 when adaptive mode is enabled.

    Returns the new interval if the pacer was bumped (so the caller can log
    it), or ``None`` if adaptive mode is off or already at the cap.
    """
    global _min_interval_s
    with _pace_lock:
        if not _pacer_adaptive:
            return None
        prev = _min_interval_s
        floor = max(prev, _PACER_ADAPT_FLOOR_S)
        new = min(_PACER_MAX_INTERVAL_S, floor * _PACER_ADAPT_FACTOR)
        if new <= prev:
            return None
        _min_interval_s = new
        return new


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value as seconds.

    Accepts either an integer/float (delta-seconds, RFC 7231) or an
    HTTP-date. Returns ``None`` when the header is missing or unparseable.
    Negative values are clamped to 0.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


def _make_request(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    max_attempts: int = _MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Issue a GET against ``API_BASE_URL + path``.

    Retries on transient 5xx and 429 with exponential backoff. Honors
    ``Retry-After`` on 429 (delta-seconds or HTTP-date), capped at
    :data:`_RETRY_AFTER_CAP_S` to prevent indefinite stalls. Returns the
    JSON-decoded payload (Polygon always returns JSON on 200).
    """
    api_params: dict[str, Any] = dict(params or {})
    api_params["apiKey"] = get_api_key()
    url = f"{API_BASE_URL}{path}"

    session = _get_session()

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        _wait_for_slot()
        try:
            response = session.get(url, params=api_params, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= max_attempts - 1:
                raise PolygonError(
                    f"Polygon request failed after {max_attempts} attempts: {exc}"
                ) from exc
            _sleep_backoff(attempt)
            continue

        status = response.status_code

        if status == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise PolygonError(
                    f"Polygon returned non-JSON 200 for {path}: {exc}"
                ) from exc

        if status in (401, 403):
            raise PolygonAuthError(
                f"Polygon auth failed for {path}: {status} {response.text[:200]}"
            )

        if status == 404:
            raise PolygonNotFoundError(f"Polygon 404 for {path}: {response.text[:200]}")

        if status in _TRANSIENT_HTTP_STATUSES:
            if attempt >= max_attempts - 1:
                if status == 429:
                    raise PolygonRateLimitError(
                        f"Polygon rate limit hit on {path} after {max_attempts} attempts"
                    )
                raise PolygonError(
                    f"Polygon transient {status} on {path} after {max_attempts} attempts: "
                    f"{response.text[:200]}"
                )
            if status == 429:
                new_interval = _adapt_on_rate_limit()
                if new_interval is not None:
                    # Visible at INFO so users see the pacer self-tuning.
                    import logging as _logging
                    _logging.getLogger(__name__).info(
                        "polygon pacer: bumped min interval to %.2fs after 429 on %s",
                        new_interval,
                        path,
                    )
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                time.sleep(min(retry_after, _RETRY_AFTER_CAP_S) + random.random())
            else:
                _sleep_backoff(attempt)
            continue

        raise PolygonError(
            f"Polygon error {status} on {path}: {response.text[:200]}"
        )

    raise PolygonError(
        f"Polygon request failed after {max_attempts} attempts (last error: {last_exc})"
    )


def paginated_results(
    initial_path: str,
    initial_params: dict[str, Any] | None = None,
    *,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Iterate Polygon's ``next_url``-style cursor pagination.

    Returns the union of ``results`` arrays across all visited pages. Caps
    at ``max_pages`` to bound runaway crawls (financials/news for a single
    ticker rarely need more than 2-3 pages).
    """
    from urllib.parse import urlparse, parse_qs

    out: list[dict[str, Any]] = []
    payload = _make_request(initial_path, initial_params)
    out.extend(payload.get("results") or [])

    next_url = payload.get("next_url")
    pages_visited = 1
    while next_url and pages_visited < max_pages:
        parsed = urlparse(next_url)
        if not parsed.path:
            break
        # Strip apiKey from any prior url; _make_request will append a fresh one.
        next_params: dict[str, Any] = {
            k: v[0] if isinstance(v, list) and len(v) == 1 else v
            for k, v in parse_qs(parsed.query).items()
            if k != "apiKey"
        }
        payload = _make_request(parsed.path, next_params)
        out.extend(payload.get("results") or [])
        next_url = payload.get("next_url")
        pages_visited += 1
    return out
