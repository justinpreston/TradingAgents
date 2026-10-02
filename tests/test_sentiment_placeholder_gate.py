"""The sentiment analyst's fabrication gate must see upstream's placeholders.

The gate bypasses the LLM when the three pre-fetched sources carry fewer than
``_MIN_INFORMATIVE_CHARS`` of real text. Upstream v0.5.x reports absences with
new wording (``<Reddit unavailable: ...>``, ``coverage_gap``'s
``<X unavailable for A..B: ...>``, and the router's ``NO_DATA_AVAILABLE`` /
``DATA_UNAVAILABLE`` sentinels). Each of those is long enough that, if counted
as content, an all-placeholder input would clear the threshold and the model
would be asked to write sentiment from nothing.
"""
from __future__ import annotations

import pytest

from tradingagents.agents.analysts import sentiment_analyst as sa
from tradingagents.dataflows.date_window import coverage_gap

pytestmark = pytest.mark.unit

_ROUTER_NO_DATA = (
    "NO_DATA_AVAILABLE: No usable market data for 'ZZZZ' from any configured vendor "
    "(news unavailable: timeout). The symbol may be invalid, delisted, not covered, "
    "or the vendor returned stale data. Do not estimate or fabricate values — report "
    "that data is unavailable for this symbol."
)
_ROUTER_UNAVAILABLE = (
    "DATA_UNAVAILABLE: no configured vendor could serve get_news right now "
    "(Yahoo Finance is unreachable). This says nothing about the instrument; report "
    "the data as unavailable and do not estimate or fabricate values."
)
_REDDIT_DOWN = "<Reddit unavailable: fetch failed (HTTP 429); this is not an absence of discussion>"


def test_upstream_placeholders_count_as_no_information():
    stocktwits = coverage_gap((), "2026-09-01", "2026-09-08", "StockTwits", "posts") or \
        "<StockTwits unavailable for 2026-09-01..2026-09-08: no items, so this is not an absence of posts>"
    for news in (_ROUTER_NO_DATA, _ROUTER_UNAVAILABLE):
        assert sa._informative_chars(news, stocktwits, _REDDIT_DOWN) == 0


def test_real_posts_still_count():
    posts = "\n".join(f"$ZZZZ post {i}: guidance raise looks credible" for i in range(12))
    assert sa._informative_chars(_ROUTER_NO_DATA, posts, _REDDIT_DOWN) >= sa._MIN_INFORMATIVE_CHARS


def test_a_line_that_merely_mentions_brackets_is_content():
    assert sa._informative_chars("<b>earnings beat</b> and raised guidance, shares up") > 0
    assert not sa._is_placeholder_line("Analysts <cautious> after the print")
