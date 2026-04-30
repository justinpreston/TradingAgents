"""Tests for ``resolve_trade_date`` and runner wiring.

The resolver guards a real foot-gun: stale hardcoded ``DEFAULT_DATE``
constants in the multi-ticker runners that anchored every run to
2024-05-10 long after that date stopped being "today". A future date
silently becomes a backtest with no data leakage protection (the
strict-PIT vendor still works "as-of" that date, but the operator no
longer knows what they're really looking at).

These tests cover:

* ``resolve_trade_date``: today / explicit past / future-rejection /
  malformed-rejection / boundary labels.
* Runner wiring: ``_parse_args`` defaults flow through the resolver and
  no runner reaches an LLM when given a future date.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tradingagents.dataflows.utils import resolve_trade_date


class TestResolveTradeDate(unittest.TestCase):
    """Pure resolver behaviour — no I/O, no monkeypatching of clock."""

    FIXED_TODAY = date(2026, 4, 29)

    def test_none_returns_today(self) -> None:
        d, label = resolve_trade_date(None, today=self.FIXED_TODAY)
        self.assertEqual(d, "2026-04-29")
        self.assertEqual(label, "today")

    def test_empty_string_returns_today(self) -> None:
        d, label = resolve_trade_date("", today=self.FIXED_TODAY)
        self.assertEqual(d, "2026-04-29")
        self.assertEqual(label, "today")

    def test_whitespace_returns_today(self) -> None:
        d, label = resolve_trade_date("   ", today=self.FIXED_TODAY)
        self.assertEqual(d, "2026-04-29")
        self.assertEqual(label, "today")

    def test_explicit_today_label(self) -> None:
        d, label = resolve_trade_date("2026-04-29", today=self.FIXED_TODAY)
        self.assertEqual(d, "2026-04-29")
        self.assertEqual(label, "today")

    def test_one_day_ago_is_backtest(self) -> None:
        d, label = resolve_trade_date("2026-04-28", today=self.FIXED_TODAY)
        self.assertEqual(d, "2026-04-28")
        self.assertEqual(label, "backtest (1d ago)")

    def test_old_backtest_label(self) -> None:
        # 2024-05-10 → 2026-04-29 = 719 days
        d, label = resolve_trade_date("2024-05-10", today=self.FIXED_TODAY)
        self.assertEqual(d, "2024-05-10")
        self.assertEqual(label, "backtest (719d ago)")

    def test_future_date_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_trade_date("2026-04-30", today=self.FIXED_TODAY)
        self.assertIn("future", str(ctx.exception).lower())
        self.assertIn("2026-04-30", str(ctx.exception))
        self.assertIn("2026-04-29", str(ctx.exception))

    def test_far_future_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_trade_date("2999-12-31", today=self.FIXED_TODAY)

    def test_malformed_date_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_trade_date("not-a-date", today=self.FIXED_TODAY)
        self.assertIn("YYYY-MM-DD", str(ctx.exception))
        # Error message should include the system date so users can see
        # what "today" the runner thinks it is.
        self.assertIn("2026-04-29", str(ctx.exception))

    def test_us_format_rejected(self) -> None:
        # Common typo — make sure it doesn't slip through.
        with self.assertRaises(ValueError):
            resolve_trade_date("04/29/2026", today=self.FIXED_TODAY)

    def test_short_year_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_trade_date("26-04-29", today=self.FIXED_TODAY)

    def test_default_today_uses_real_clock(self) -> None:
        # When ``today`` is omitted, must fall back to ``date.today()``.
        d, label = resolve_trade_date(None)
        self.assertEqual(d, date.today().strftime("%Y-%m-%d"))
        self.assertEqual(label, "today")

    def test_strips_trailing_whitespace(self) -> None:
        d, _ = resolve_trade_date("  2024-05-10  ", today=self.FIXED_TODAY)
        self.assertEqual(d, "2024-05-10")

    def test_label_boundary_yesterday(self) -> None:
        yesterday = self.FIXED_TODAY - timedelta(days=1)
        _, label = resolve_trade_date(yesterday.isoformat(), today=self.FIXED_TODAY)
        self.assertEqual(label, "backtest (1d ago)")


class _RunnerWiringTestBase:
    """Mixin for asserting all 3 multi-ticker runners route through the
    resolver. Subclasses set ``MODULE_NAME``.

    Some runner scripts in this repo are local-only dev harnesses (not
    tracked by git). When the file is absent the wiring tests skip
    rather than fail — the resolver tests above are sufficient to cover
    the core behaviour. The persona-aligned runner *is* tracked so its
    coverage is non-skippable.
    """

    MODULE_NAME: str = ""
    REQUIRED: bool = False

    def _import_runner(self):
        # Re-import fresh so module-level ``DEFAULT_DATE`` is current.
        if self.MODULE_NAME in sys.modules:
            del sys.modules[self.MODULE_NAME]
        try:
            return importlib.import_module(self.MODULE_NAME)
        except ImportError:
            if self.REQUIRED:
                raise
            self.skipTest(f"{self.MODULE_NAME} not present in checkout")

    def test_default_date_is_none(self) -> None:
        mod = self._import_runner()
        # ``None`` default → resolver picks today. The string sentinel
        # ``"2024-05-10"`` would re-introduce the stale-anchor bug.
        self.assertIsNone(
            mod.DEFAULT_DATE,
            f"{self.MODULE_NAME}.DEFAULT_DATE must be None so resolver returns today; "
            f"got {mod.DEFAULT_DATE!r}",
        )

    def test_argparse_uses_default_date(self) -> None:
        mod = self._import_runner()
        with mock.patch.object(sys, "argv", [self.MODULE_NAME]):
            args = mod._parse_args()
        self.assertEqual(args.date, mod.DEFAULT_DATE)


class TestPersonaAlignedWiring(_RunnerWiringTestBase, unittest.TestCase):
    MODULE_NAME = "run_copilot_persona_aligned"
    REQUIRED = True  # Tracked in git; absence is a real failure.


class TestCopilotMultiWiring(_RunnerWiringTestBase, unittest.TestCase):
    MODULE_NAME = "run_copilot_multi"


class TestCopilotOpusMultiWiring(_RunnerWiringTestBase, unittest.TestCase):
    MODULE_NAME = "run_copilot_opus_multi"


class TestRunnerRejectsFutureDate(unittest.TestCase):
    """End-to-end: future ``--date`` exits 2 before any LLM call."""

    def _run_with_future_date(self, module_name: str, *, required: bool = False) -> int:
        if module_name in sys.modules:
            del sys.modules[module_name]
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            if required:
                raise
            self.skipTest(f"{module_name} not present in checkout")
        future = (date.today() + timedelta(days=30)).isoformat()
        with (
            mock.patch.object(sys, "argv", [module_name, "--date", future, "NVDA"]),
            mock.patch.object(mod, "_resolve_github_token", return_value="x"),
            mock.patch.object(mod, "TradingAgentsGraph") as graph_cls,
        ):
            rc = mod.main()
            self.assertFalse(
                graph_cls.called,
                f"{module_name} built a TradingAgentsGraph despite future date",
            )
        return rc

    def test_persona_aligned_rejects_future(self) -> None:
        self.assertEqual(
            self._run_with_future_date("run_copilot_persona_aligned", required=True),
            2,
        )

    def test_copilot_multi_rejects_future(self) -> None:
        self.assertEqual(self._run_with_future_date("run_copilot_multi"), 2)

    def test_copilot_opus_multi_rejects_future(self) -> None:
        self.assertEqual(self._run_with_future_date("run_copilot_opus_multi"), 2)


if __name__ == "__main__":
    unittest.main()
