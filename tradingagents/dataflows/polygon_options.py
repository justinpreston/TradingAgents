"""Shared Polygon options-chain snapshot fetch.

Extracted from scripts/build_options_overlay.py (the ``fetch_chain``
function, formerly a private ``_fetch_chain``), which scripts/
score_picks_iv_surface.py used to import directly from a sibling script.
Both now import from here.

Retry semantics (unchanged from the original implementation — verified
against the 2026-06-26 transient-403 incident that motivated the
auth-blip retry path):

  * Rate limit (429): retry up to ``max_retries`` (default 3) total
    attempts, sleeping 70s between retries.
  * Auth blip (403 / NOT_AUTHORIZED): retry up to ``max_auth_retries``
    (2) times, sleeping ``auth_retry_backoff`` (default 10s) between
    retries. Polygon occasionally returns a stray 403 that clears on
    retry rather than indicating a real entitlement problem.
"""

from __future__ import annotations

import time

from tradingagents.dataflows.polygon_common import _make_request


def _is_rate_limit_error(msg: str) -> bool:
    return "rate" in msg or "429" in msg


def _is_auth_blip_error(msg: str) -> bool:
    """Transient Polygon entitlement blips (e.g. a stray 403 NOT_AUTHORIZED)
    that clear on retry. On 2026-06-26 a transient 403 failed every single
    chain fetch in the run with no retry — this classifier lets us retry a
    couple of times with a short backoff instead of immediately giving up.
    """
    return "403" in msg or "not_authorized" in msg or "not authorized" in msg


def fetch_chain(ticker: str, contract_type: str,
                strike_min: float, strike_max: float,
                exp_min: str, exp_max: str,
                limit: int = 250, max_retries: int = 3,
                auth_retry_backoff: float = 10.0) -> tuple[list[dict], str | None]:
    """Fetch a Polygon options-chain snapshot for ``ticker``.

    Returns ``(contracts, error_msg)``. Empty list with ``error_msg`` set
    means a real failure; empty list with ``error_msg`` is ``None`` means
    simply no contracts matched the window.
    """
    params = {
        "contract_type": contract_type,
        "strike_price.gte": strike_min,
        "strike_price.lte": strike_max,
        "expiration_date.gte": exp_min,
        "expiration_date.lte": exp_max,
        "limit": limit,
    }
    last_err = None
    auth_retries = 0
    max_auth_retries = 2
    for attempt in range(max_retries):
        try:
            r = _make_request(f"/v3/snapshot/options/{ticker}", params)
            return (r.get("results", []) or []), None
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            msg = last_err.lower()
            if _is_rate_limit_error(msg) and attempt < max_retries - 1:
                time.sleep(70)
                continue
            if _is_auth_blip_error(msg) and auth_retries < max_auth_retries:
                auth_retries += 1
                time.sleep(auth_retry_backoff)
                continue
            return [], last_err
    return [], last_err
