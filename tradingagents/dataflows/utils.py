import os
import re
import json
import pandas as pd
from datetime import date, timedelta, datetime
from typing import Annotated

SavePathType = Annotated[str, "File path to save data. If None, data is not saved."]

# Tickers can contain letters, digits, dot, dash, underscore, and caret
# (for index symbols like ^GSPC). Anything else is rejected so the value
# never escapes a containing directory when interpolated into a path.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


def save_output(data: pd.DataFrame, tag: str, save_path: SavePathType = None) -> None:
    if save_path:
        data.to_csv(save_path, encoding="utf-8")
        print(f"{tag} saved to {save_path}")


def get_current_date():
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


def decorate_all_methods(decorator):
    def class_decorator(cls):
        for attr_name, attr_value in cls.__dict__.items():
            if callable(attr_value):
                setattr(cls, attr_name, decorator(attr_value))
        return cls

    return class_decorator


def get_next_weekday(date):

    if not isinstance(date, datetime):
        date = datetime.strptime(date, "%Y-%m-%d")

    if date.weekday() >= 5:
        days_to_add = 7 - date.weekday()
        next_weekday = date + timedelta(days=days_to_add)
        return next_weekday
    else:
        return date
