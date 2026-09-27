"""Shared look-ahead-safe date-window filtering for dated content.

News, StockTwits, and Reddit all pull recent items that must be trimmed to the
analysis window so a historical/backtest run never sees content published after
its as-of date. Centralizing the rule keeps every source consistent (#1126,
#1220): every timestamp is normalized to UTC, the upper bound is exclusive at
midnight after ``end`` (so an item stamped exactly then can't leak), and an
undated item is kept only when the window reaches the present (a live run), since
in a backtest we can't prove it isn't future.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware; a naive value is assumed to be UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def in_window(pub_dt: datetime | None, start_dt: datetime, end_dt: datetime) -> bool:
    """Whether an item belongs in the half-open window ``[start, end + 1 day)``.

    ``pub_dt`` None means undated: kept only when the window reaches the present.
    """
    end = to_utc(end_dt)
    if pub_dt is not None:
        return to_utc(start_dt) <= to_utc(pub_dt) < end + timedelta(days=1)
    return end >= datetime.now(timezone.utc) - timedelta(days=1)


def get_current_date() -> str:
    """Today's date, YYYY-MM-DD."""
    return date.today().strftime("%Y-%m-%d")



def resolve_trade_date(value, *, today=None):
    """Resolve and validate a trade date.

    ``value`` may be ``None`` / empty (falls back to today's system date) or
    an ISO ``YYYY-MM-DD`` string. Future dates are refused — running an
    analysis "as-of" a date the data layer cannot have observed is a bug.

    Returns a ``(canonical_date, label)`` tuple where ``label`` is
    ``"today"`` for delta=0 and ``"backtest ({N}d ago)"`` otherwise. The
    label is operator-facing only (banner/manifest); it is *not* propagated
    into agent prompts so PIT discipline is preserved.

    Raises ``ValueError`` on malformed input or future dates.
    """
    today = today or date.today()
    if value is None or not str(value).strip():
        return today.strftime("%Y-%m-%d"), "today"
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"--date must be ISO YYYY-MM-DD (got {value!r}). "
            f"System date is {today.isoformat()}."
        ) from exc
    if parsed > today:
        raise ValueError(
            f"Trade date {parsed.isoformat()} is in the future "
            f"(system date is {today.isoformat()}). Refusing to run."
        )
    if parsed == today:
        return parsed.isoformat(), "today"
    delta = (today - parsed).days
    return parsed.isoformat(), f"backtest ({delta}d ago)"


def coverage_gap(
    dates, start_date: str, end_date: str, source: str, subject: str
) -> str | None:
    """Placeholder for a window a feed did not fully observe, else None.

    Yahoo news and the Reddit and StockTwits feeds return their latest items
    whatever window is asked for, so "none found" over a window they never
    observed would claim an absence nobody saw. A window is observed when
    coverage reaches its first day and it ends by today; an empty result is then
    a real absence and this returns None.

    ``dates`` are the returned items' timestamps, plus the lookback start for a
    feed with a fixed lookback. The oldest one bounds coverage only for a feed
    returned newest-first and unbroken in time; a merged or relevance-ranked
    result passes no dates, leaving only the present as the bound.
    """
    now = datetime.now(timezone.utc)
    oldest = min((to_utc(d) for d in dates if d is not None), default=now)
    if datetime.strptime(end_date, "%Y-%m-%d").date() > now.date():
        reason = "the window extends past today"
    elif oldest.date() > datetime.strptime(start_date, "%Y-%m-%d").date():
        reason = "it only serves recent items"
    else:
        return None
    return f"<{source} unavailable for {start_date}..{end_date}: {reason}, so this is not an absence of {subject}>"


def _parse(date: str | None) -> datetime | None:
    try:
        return datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def as_of(requested: str | None, trade_date: str) -> str | None:
    """The date a tool serves: the model's date, but never later than the run's.

    A model can omit the date or pass today's instead of the analysis date, which
    would walk past every point-in-time guard behind the tool. An empty
    ``trade_date`` (a direct call outside a graph run) passes the request through.
    """
    if not trade_date:
        return requested
    parsed = _parse(requested)
    return requested if parsed is not None and parsed <= _parse(trade_date) else trade_date


def as_of_window(start_date: str, end_date: str, trade_date: str) -> tuple[str, str]:
    """``[start, end]`` with its end clamped to the run date.

    A window wholly after the run date keeps its length and moves back to end there.
    """
    end = as_of(end_date, trade_date)
    start, old_end = _parse(start_date), _parse(end_date)
    if end == end_date or start is None or start <= _parse(end):
        return start_date, end
    span = (old_end - start) if old_end is not None and old_end >= start else timedelta(0)
    return f"{_parse(end) - span:%Y-%m-%d}", end


def withhold_live_profile(curr_date: str | None, label: str) -> str | None:
    """Notice to serve instead of a live-only company profile, or None to serve it.

    Vendor "company overview" endpoints (yfinance ``Ticker.info``, Alpha Vantage
    ``OVERVIEW``) carry no historical vintage — not even name, sector and
    industry, which move when a company renames or is reclassified — so serving
    one into a run dated in the past leaks post-decision information (#1300).
    Every fundamentals vendor withholds on this rule, so switching between them
    cannot reintroduce the leak.
    """
    if not curr_date:
        return None
    today = get_current_date()
    if curr_date >= today:
        return None
    return (
        f"# Company Fundamentals for {label}\n"
        f"# Point-in-time as of: {curr_date}\n\n"
        f"Profile fundamentals are withheld for this date. This vendor serves "
        f"only present-day values with no historical vintage: market "
        f"cap, valuation multiples, the 52-week range and TTM income move with "
        f"today's quote, and even the name, sector and industry reflect today "
        f"rather than {curr_date} (companies rename and get reclassified). "
        f"Serving them would put post-decision information into a {curr_date} "
        f"analysis. Point-in-time fundamentals for {curr_date} are available "
        f"from the balance sheet, income statement, and cash flow tools."
    )
