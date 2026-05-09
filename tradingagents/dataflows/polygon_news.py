"""Polygon news + insider-transactions endpoints.

Both endpoints accept date-range filters and date-stamped responses, making
them strict-PIT for backtest replay. ``get_news`` and ``get_global_news``
mirror the yfinance-side signatures so the vendor router can swap providers
without touching agent code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from .polygon_common import (
    PolygonError,
    PolygonNotFoundError,
    _make_request,
    paginated_results,
)


def _format_news_entries(entries: list[dict[str, Any]], header_label: str) -> str:
    if not entries:
        return f"# {header_label}\n# No news found in the requested window\n"

    lines = [f"# {header_label}", f"# Source: Polygon", f"# Total items: {len(entries)}\n"]
    for item in entries:
        published = item.get("published_utc", "")
        title = item.get("title", "(untitled)")
        publisher_obj = item.get("publisher") or {}
        publisher = publisher_obj.get("name", "Unknown") if isinstance(publisher_obj, dict) else str(publisher_obj)
        url = item.get("article_url") or item.get("url") or ""
        description = item.get("description", "") or ""
        # Trim long descriptions to keep the prompt reasonably sized
        if len(description) > 500:
            description = description[:500].rsplit(" ", 1)[0] + "..."
        keywords = ", ".join(item.get("keywords", []) or [])

        lines.append(f"## {title}")
        lines.append(f"- Published: {published}")
        lines.append(f"- Publisher: {publisher}")
        if keywords:
            lines.append(f"- Keywords: {keywords}")
        if url:
            lines.append(f"- URL: {url}")
        if description:
            lines.append(f"- Summary: {description}")
        lines.append("")

    return "\n".join(lines)


def _date_window(curr_date: str | None, lookback_days: int = 7) -> tuple[str, str]:
    """Default to a 7-day window ending on curr_date (or today)."""
    if curr_date:
        end_dt = datetime.strptime(curr_date[:10], "%Y-%m-%d")
    else:
        end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=lookback_days)
    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


def get_news(
    ticker: Annotated[str, "ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch ticker-specific news from Polygon's news endpoint.

    Filters to articles published in ``[start_date, end_date)`` (PIT-safe
    when the caller passes a backtest-aware end_date).
    """
    params: dict[str, Any] = {
        "ticker": ticker.upper(),
        "published_utc.gte": start_date,
        "published_utc.lt": end_date,
        "order": "desc",
        "limit": 50,
    }
    try:
        results = paginated_results("/v2/reference/news", params, max_pages=2)
    except PolygonNotFoundError:
        return f"No news found for {ticker} between {start_date} and {end_date}"
    except PolygonError as exc:
        return f"Error retrieving news for {ticker}: {exc}"

    return _format_news_entries(
        results,
        f"News for {ticker.upper()} from {start_date} to {end_date}",
    )


def get_global_news(
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"],
    look_back_days: Annotated[int, "how many days of news to fetch"] = 7,
    limit: Annotated[int, "max articles to return"] = 10,
) -> str:
    """Fetch broad market news (no ticker filter) from Polygon."""
    start_date, end_date = _date_window(curr_date, look_back_days)

    params: dict[str, Any] = {
        "published_utc.gte": start_date,
        "published_utc.lt": end_date,
        "order": "desc",
        "limit": min(int(limit) * 5, 50),  # over-fetch then trim to limit
    }
    try:
        results = paginated_results("/v2/reference/news", params, max_pages=2)
    except PolygonError as exc:
        return f"Error retrieving global news: {exc}"

    return _format_news_entries(
        results[: int(limit)],
        f"Global market news from {start_date} to {end_date}",
    )


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """Insider Form-4 transactions from Polygon.

    Polygon does not currently expose an SEC insider-transactions endpoint
    on the public REST surface — we have probed every documented spelling
    (``/v3/reference/insider_transactions``,
    ``/vX/reference/insiders/transactions``, the post-rebrand
    ``api.massive.com/v3/...`` variants, etc.) and they all return ``404
    page not found`` rather than the ``403 NOT_AUTHORIZED`` JSON Polygon
    uses for paywalled-but-real URLs.

    Rather than hard-coding that absence (which would silently keep
    failing if Polygon ships the endpoint later, or if the user upgrades
    to a plan/account that has it), we attempt the request and let
    :class:`PolygonError` bubble up so the vendor router in
    :mod:`tradingagents.dataflows.interface` falls through to
    alpha_vantage / yfinance. ``404``, ``403``, ``401``, ``5xx``, and
    network errors are all already mapped to ``PolygonError`` subclasses
    by :func:`_make_request`, so the runtime semantics here are
    "try, fall through on any failure". The cost of a failing probe is a
    single HTTP round-trip — bounded and acceptable.

    If/when Polygon adds the endpoint, update ``_INSIDER_TXN_ENDPOINTS``
    to include the active path; the first 200 response wins.
    """
    last_exc: PolygonError | None = None
    for path in _INSIDER_TXN_ENDPOINTS:
        try:
            results = paginated_results(
                path, {"ticker": ticker.upper(), "limit": 50}, max_pages=1
            )
        except PolygonError as exc:
            last_exc = exc
            continue
        return _format_insider_transactions(ticker.upper(), results)
    raise PolygonError(
        "Polygon insider transactions unavailable on every known endpoint "
        f"spelling — last error: {last_exc}"
    )


_INSIDER_TXN_ENDPOINTS: tuple[str, ...] = (
    "/v3/reference/insider_transactions",
    "/vX/reference/insider_transactions",
)


def _format_insider_transactions(ticker: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f"No insider transactions found for {ticker}."
    lines = [f"Insider transactions for {ticker} ({len(results)} entries):", ""]
    for r in results:
        date = r.get("transaction_date") or r.get("filing_date") or "?"
        name = r.get("executive") or r.get("filer_name") or "?"
        title = r.get("executive_title") or r.get("filer_title") or ""
        action = r.get("acquisition_or_disposal") or r.get("transaction_type") or "?"
        shares = r.get("shares") or r.get("quantity") or "?"
        price = r.get("price_per_share") or r.get("price") or "?"
        title_blurb = f" ({title})" if title else ""
        lines.append(
            f"- {date} {name}{title_blurb}: {action} {shares} @ {price}"
        )
    return "\n".join(lines)
