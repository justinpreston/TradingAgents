import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from AlgorithmImports import *


@dataclass
class SignalSpec:
    id: str
    ticker: str
    tier: str
    classification: str
    underlying_ref_price: Optional[float]
    right: str
    strike: float
    expiry: date
    target_delta: Optional[float]
    occ_symbol: str
    ref_premium_per_share: Optional[float]
    max_premium_per_share: float
    size_pct: float
    max_size_pct: float
    cons_pt: Optional[float]
    aggr_pt: Optional[float]
    rule: str
    stop_loss_premium_pct: float
    time_stop_dte: int
    approved: bool
    notes: str = ""


@dataclass
class PositionState:
    signal: SignalSpec
    underlying_symbol: Any
    option_symbol: Any
    entry_order_id: Optional[int] = None
    entry_fill_price: Optional[float] = None
    entry_quantity: int = 0
    cons_leg_fired: bool = False
    aggr_leg_fired: bool = False
    closed: bool = False
    exit_order_ids: List[int] = field(default_factory=list)


class TradingAgentsAlgorithm(QCAlgorithm):
    """Executes TradingAgents approved long-call signals with tiered exits."""

    def Initialize(self):
        self.SetStartDate(2026, 6, 26)
        self.SetEndDate(2027, 1, 31)
        self.SetCash(100000)

        self.signal_specs: Dict[str, SignalSpec] = {}
        self.pending_signals: Dict[str, SignalSpec] = {}
        self.positions_by_id: Dict[str, PositionState] = {}
        self.order_to_signal: Dict[int, str] = {}
        self.underlying_symbols: Dict[str, Any] = {}
        self.option_roots: Dict[str, Any] = {}
        self.latest_chains: Dict[str, Any] = {}
        self.daily_highs: Dict[Any, float] = {}
        self.daily_high_date: Optional[date] = None

        self.default_stop_loss_pct = self._get_float_parameter("stop-loss-pct", -0.40)
        self.default_time_stop_dte = self._get_int_parameter("time-stop-dte", 21)
        signals_path = self.GetParameter("signals-path") or "signals.json"

        signals = self._load_signals(signals_path)
        for signal in signals:
            self._register_signal(signal)

        if self.underlying_symbols:
            first_symbol = next(iter(self.underlying_symbols.values()))
            self.SetBenchmark(first_symbol)
        else:
            self.SetBenchmark("SPY")

        self.Schedule.On(
            self.DateRules.EveryDay(),
            self.TimeRules.BeforeMarketClose("SPY", 5),
            self.ManageExits,
        )
        self.Debug(f"Loaded {len(self.signal_specs)} approved TradingAgents signals from {signals_path}")

    def OnData(self, data):
        self._roll_daily_highs_if_needed()
        self._update_underlying_highs(data)
        self._cache_option_chains(data)
        self._try_enter_pending_signals(data)

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status != OrderStatus.Filled:
            return

        signal_id = self.order_to_signal.get(orderEvent.OrderId)
        if not signal_id:
            return

        state = self.positions_by_id.get(signal_id)
        if state is None:
            return

        fill_qty = int(orderEvent.FillQuantity)
        fill_price = float(orderEvent.FillPrice)
        if fill_qty > 0:
            total_cost = (state.entry_fill_price or 0.0) * state.entry_quantity + fill_price * fill_qty
            state.entry_quantity += fill_qty
            state.entry_fill_price = total_cost / max(1, state.entry_quantity)
            self.Log(f"ENTRY filled {signal_id}: +{fill_qty} {state.option_symbol} @ {fill_price:.2f}/sh")
            return

        self.Log(f"EXIT filled {signal_id}: {fill_qty} {state.option_symbol} @ {fill_price:.2f}/sh")
        remaining = self._current_contract_quantity(state)
        if remaining <= 0:
            state.closed = True
            self.Log(f"POSITION closed {signal_id}")

    def ManageExits(self):
        self._roll_daily_highs_if_needed()
        for signal_id, state in list(self.positions_by_id.items()):
            if state.closed or state.entry_fill_price is None:
                continue

            quantity = self._current_contract_quantity(state)
            if quantity <= 0:
                state.closed = True
                continue

            signal = state.signal
            underlying_high = self.daily_highs.get(state.underlying_symbol)
            if underlying_high is None:
                underlying_high = self.Securities[state.underlying_symbol].Price
                self.Debug(f"{signal_id}: no daily high cached; using underlying price {underlying_high:.2f}")

            remaining_after_targets = self._apply_target_exits(state, quantity, float(underlying_high))
            if remaining_after_targets <= 0:
                continue

            if self._apply_universal_exits(state, remaining_after_targets):
                continue

            self.Debug(
                f"HOLD {signal_id}: high={underlying_high:.2f}, contracts={remaining_after_targets}, "
                f"entry={state.entry_fill_price:.2f}"
            )

    def _load_signals(self, path: str) -> List[SignalSpec]:
        candidate_paths = [path]
        if not os.path.isabs(path):
            candidate_paths.append(os.path.join(os.getcwd(), path))
            candidate_paths.append(os.path.join(os.getcwd(), "lean", "algorithm", path))
            candidate_paths.append(os.path.join(os.getcwd(), "lean", path))

        manifest = None
        chosen_path = None
        for candidate in candidate_paths:
            if candidate and os.path.exists(candidate):
                chosen_path = candidate
                with open(candidate, "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
                break

        if manifest is None:
            self.Log(f"No TradingAgents signals file found at {path}; algorithm will not enter trades")
            return []

        if manifest.get("schema_version") != "1.0":
            self.Log(f"Unsupported or missing signals schema_version in {chosen_path}; expected 1.0")

        parsed = []
        for raw in manifest.get("signals", []):
            if not raw.get("approved", False):
                continue
            try:
                parsed.append(self._parse_signal(raw))
            except Exception as exc:
                self.Log(f"Skipping malformed signal {raw.get('id', '<unknown>')}: {exc}")
        return parsed

    def _parse_signal(self, raw: Dict[str, Any]) -> SignalSpec:
        option = raw.get("option", {})
        entry = raw.get("entry", {})
        exits = raw.get("exits", {})
        expiry = datetime.strptime(option["expiry"], "%Y-%m-%d").date()
        return SignalSpec(
            id=str(raw["id"]),
            ticker=str(raw["ticker"]).upper(),
            tier=str(raw.get("tier", "")).upper(),
            classification=str(raw.get("classification", "")),
            underlying_ref_price=self._optional_float(raw.get("underlying_ref_price")),
            right=str(option.get("right", "call")).lower(),
            strike=float(option["strike"]),
            expiry=expiry,
            target_delta=self._optional_float(option.get("target_delta")),
            occ_symbol=str(option.get("occ_symbol", "")),
            ref_premium_per_share=self._optional_float(option.get("ref_premium_per_share")),
            max_premium_per_share=float(entry["max_premium_per_share"]),
            size_pct=float(entry.get("size_pct", 0.04)),
            max_size_pct=float(entry.get("max_size_pct", entry.get("size_pct", 0.04))),
            cons_pt=self._optional_float(exits.get("cons_pt")),
            aggr_pt=self._optional_float(exits.get("aggr_pt")),
            rule=str(exits.get("rule", "")),
            stop_loss_premium_pct=float(exits.get("stop_loss_premium_pct", self.default_stop_loss_pct)),
            time_stop_dte=int(exits.get("time_stop_dte", self.default_time_stop_dte)),
            approved=bool(raw.get("approved", False)),
            notes=str(raw.get("notes", "")),
        )

    def _register_signal(self, signal: SignalSpec):
        if signal.right != "call":
            self.Log(f"Skipping {signal.id}: only call options are supported, got {signal.right}")
            return
        if signal.classification != "PICK":
            self.Log(f"Skipping {signal.id}: classification is {signal.classification}, not PICK")
            return
        if signal.expiry <= self.Time.date():
            self.Log(f"Skipping {signal.id}: option expired on {signal.expiry}")
            return

        equity = self.AddEquity(signal.ticker, Resolution.Daily)
        underlying_symbol = equity.Symbol
        option = self.AddOption(signal.ticker, Resolution.Minute)
        option.SetFilter(lambda universe, s=signal: universe.Strikes(-20, 20).Expiration(0, max(1, (s.expiry - self.Time.date()).days + 7)).CallsOnly())

        self.signal_specs[signal.id] = signal
        self.pending_signals[signal.id] = signal
        self.underlying_symbols[signal.ticker] = underlying_symbol
        self.option_roots[signal.ticker] = option.Symbol
        self.Log(f"Registered {signal.id}: {signal.ticker} {signal.expiry} {signal.strike}C tier={signal.tier}")

    def _try_enter_pending_signals(self, data):
        for signal_id, signal in list(self.pending_signals.items()):
            if signal_id in self.positions_by_id and self.positions_by_id[signal_id].entry_order_id is not None:
                continue
            if signal.expiry <= self.Time.date():
                self.Log(f"Skipping {signal_id}: expired before entry")
                self.pending_signals.pop(signal_id, None)
                continue

            contract_symbol = self._resolve_contract_symbol(signal)
            if contract_symbol is None:
                self.Debug(f"Waiting for tradable option contract for {signal_id}")
                continue

            if not self.Securities.ContainsKey(contract_symbol):
                self.AddOptionContract(contract_symbol, Resolution.Minute)

            security = self.Securities[contract_symbol]
            ask_price = self._option_ask_price(contract_symbol)
            if ask_price <= 0:
                self.Debug(f"Waiting for ask price for {signal_id} {contract_symbol}")
                continue

            max_budget = self.Portfolio.TotalPortfolioValue * max(signal.max_size_pct, signal.size_pct)
            desired_budget = self.Portfolio.TotalPortfolioValue * signal.size_pct
            contract_cost = ask_price * 100.0
            contracts = int(math.floor(desired_budget / contract_cost))
            if contracts < 1 and contract_cost <= max_budget and self.Portfolio.Cash >= contract_cost:
                contracts = 1
            if contracts < 1:
                self.Log(f"Skipping {signal_id}: unaffordable at ask {ask_price:.2f}/sh")
                self.pending_signals.pop(signal_id, None)
                continue

            max_contracts = max(1, int(math.floor(max_budget / contract_cost)))
            contracts = min(contracts, max_contracts)
            if not security.IsTradable:
                self.Debug(f"Waiting for tradable security flag for {signal_id} {contract_symbol}")
                continue

            ticket = self.LimitOrder(contract_symbol, contracts, signal.max_premium_per_share, f"TradingAgents entry {signal_id}")
            state = PositionState(signal, self.underlying_symbols[signal.ticker], contract_symbol, ticket.OrderId)
            self.positions_by_id[signal_id] = state
            self.order_to_signal[ticket.OrderId] = signal_id
            self.pending_signals.pop(signal_id, None)
            self.Log(
                f"ENTRY submitted {signal_id}: buy {contracts} {contract_symbol} limit "
                f"{signal.max_premium_per_share:.2f}/sh, ask={ask_price:.2f}/sh"
            )

    def _resolve_contract_symbol(self, signal: SignalSpec):
        exact = self._try_symbol_from_occ(signal)
        if exact is not None:
            return exact

        provider_symbol = self._resolve_from_option_chain_provider(signal)
        if provider_symbol is not None:
            return provider_symbol

        chain_symbol = self.option_roots.get(signal.ticker)
        chain = self.latest_chains.get(signal.ticker) or self.latest_chains.get(chain_symbol)
        if chain is None:
            return None

        candidates = []
        for contract in chain:
            symbol = contract.Symbol
            if getattr(symbol.ID, "OptionRight", None) != OptionRight.Call:
                continue
            if symbol.ID.Date.date() != signal.expiry:
                continue
            candidates.append(contract)

        if not candidates:
            return None

        strike_matches = [c for c in candidates if abs(float(c.Symbol.ID.StrikePrice) - signal.strike) < 0.01]
        if strike_matches:
            return strike_matches[0].Symbol

        if signal.target_delta is not None:
            def delta_distance(contract):
                greeks = getattr(contract, "Greeks", None)
                delta = getattr(greeks, "Delta", None) if greeks is not None else None
                return abs(abs(float(delta or 0.0)) - signal.target_delta)
            return min(candidates, key=delta_distance).Symbol

        return min(candidates, key=lambda c: abs(float(c.Symbol.ID.StrikePrice) - signal.strike)).Symbol

    def _try_symbol_from_occ(self, signal: SignalSpec):
        if not signal.occ_symbol:
            return None
        try:
            mapped = SymbolCache.GetSymbol(signal.occ_symbol)
            if mapped is not None and str(mapped) != signal.occ_symbol:
                return mapped
        except Exception:
            return None
        return None

    def _resolve_from_option_chain_provider(self, signal: SignalSpec):
        underlying = self.underlying_symbols.get(signal.ticker)
        if underlying is None:
            return None
        try:
            contracts = self.OptionChainProvider.GetOptionContractList(underlying, self.Time)
        except Exception as exc:
            self.Debug(f"OptionChainProvider unavailable for {signal.id}: {exc}")
            return None

        calls = [s for s in contracts if s.ID.OptionRight == OptionRight.Call and s.ID.Date.date() == signal.expiry]
        if not calls:
            return None
        strike_matches = [s for s in calls if abs(float(s.ID.StrikePrice) - signal.strike) < 0.01]
        if strike_matches:
            return strike_matches[0]
        return min(calls, key=lambda s: abs(float(s.ID.StrikePrice) - signal.strike))

    def _apply_target_exits(self, state: PositionState, quantity: int, underlying_high: float) -> int:
        signal = state.signal
        remaining = quantity

        if signal.rule == "tier_a_take_profit":
            if not state.cons_leg_fired and signal.cons_pt is not None and underlying_high >= signal.cons_pt:
                trim_qty = min(remaining, max(1, int(math.ceil(state.entry_quantity * 0.5))))
                self._submit_exit(state, trim_qty, "tier_a_cons_pt", underlying_high, signal.cons_pt)
                state.cons_leg_fired = True
                remaining -= trim_qty
            if remaining > 0 and not state.aggr_leg_fired and signal.aggr_pt is not None and underlying_high >= signal.aggr_pt:
                self._submit_exit(state, remaining, "tier_a_aggr_pt", underlying_high, signal.aggr_pt)
                state.aggr_leg_fired = True
                remaining = 0
        elif signal.rule == "tier_b_run":
            if not state.aggr_leg_fired and signal.aggr_pt is not None and underlying_high >= signal.aggr_pt:
                self._submit_exit(state, remaining, "tier_b_aggr_pt", underlying_high, signal.aggr_pt)
                state.aggr_leg_fired = True
                remaining = 0
        elif signal.rule == "tier_c_trim":
            if not state.aggr_leg_fired and signal.aggr_pt is not None and underlying_high >= signal.aggr_pt:
                self._submit_exit(state, remaining, "tier_c_aggr_pt", underlying_high, signal.aggr_pt)
                state.aggr_leg_fired = True
                remaining = 0
        else:
            self.Debug(f"{signal.id}: unknown exit rule {signal.rule}; only universal exits active")

        return remaining

    def _apply_universal_exits(self, state: PositionState, quantity: int) -> bool:
        signal = state.signal
        option_price = self._current_option_exit_price(state.option_symbol)
        stop_price = state.entry_fill_price * (1.0 + signal.stop_loss_premium_pct)
        if option_price > 0 and option_price <= stop_price:
            self._submit_exit(state, quantity, "stop_loss", option_price, stop_price)
            state.closed = True
            return True

        dte = (signal.expiry - self.Time.date()).days
        if dte <= signal.time_stop_dte:
            self._submit_exit(state, quantity, "time_stop", float(dte), float(signal.time_stop_dte))
            state.closed = True
            return True
        return False

    def _submit_exit(self, state: PositionState, quantity: int, rule: str, trigger_value: float, threshold: float):
        quantity = int(quantity)
        if quantity <= 0:
            return
        ticket = self.MarketOrder(state.option_symbol, -quantity, False, f"TradingAgents exit {state.signal.id} {rule}")
        state.exit_order_ids.append(ticket.OrderId)
        self.order_to_signal[ticket.OrderId] = state.signal.id
        self.Log(
            f"EXIT submitted {state.signal.id}: sell {quantity} {state.option_symbol}, rule={rule}, "
            f"trigger={trigger_value:.2f}, threshold={threshold:.2f}"
        )

    def _update_underlying_highs(self, data):
        for symbol in self.underlying_symbols.values():
            bar = None
            if data.Bars.ContainsKey(symbol):
                bar = data.Bars[symbol]
            elif self.Securities.ContainsKey(symbol):
                price = self.Securities[symbol].Price
                if price > 0:
                    self.daily_highs[symbol] = max(self.daily_highs.get(symbol, 0.0), float(price))
            if bar is not None:
                self.daily_highs[symbol] = max(self.daily_highs.get(symbol, 0.0), float(bar.High))

    def _cache_option_chains(self, data):
        for chain in data.OptionChains.Values:
            ticker = chain.Underlying.Symbol.Value if chain.Underlying is not None else None
            if ticker:
                self.latest_chains[ticker] = chain
            self.latest_chains[chain.Symbol] = chain

    def _roll_daily_highs_if_needed(self):
        today = self.Time.date()
        if self.daily_high_date != today:
            self.daily_highs = {}
            self.daily_high_date = today

    def _current_contract_quantity(self, state: PositionState) -> int:
        if not self.Portfolio.ContainsKey(state.option_symbol):
            return 0
        return int(self.Portfolio[state.option_symbol].Quantity)

    def _option_ask_price(self, symbol) -> float:
        if not self.Securities.ContainsKey(symbol):
            return 0.0
        security = self.Securities[symbol]
        ask = float(getattr(security, "AskPrice", 0.0) or 0.0)
        if ask > 0:
            return ask
        return float(getattr(security, "Price", 0.0) or 0.0)

    def _current_option_exit_price(self, symbol) -> float:
        if not self.Securities.ContainsKey(symbol):
            return 0.0
        security = self.Securities[symbol]
        bid = float(getattr(security, "BidPrice", 0.0) or 0.0)
        if bid > 0:
            return bid
        return float(getattr(security, "Price", 0.0) or 0.0)

    def _get_float_parameter(self, name: str, default: float) -> float:
        value = self.GetParameter(name)
        try:
            return float(value) if value not in (None, "") else default
        except ValueError:
            self.Log(f"Invalid parameter {name}={value}; using {default}")
            return default

    def _get_int_parameter(self, name: str, default: int) -> int:
        value = self.GetParameter(name)
        try:
            return int(value) if value not in (None, "") else default
        except ValueError:
            self.Log(f"Invalid parameter {name}={value}; using {default}")
            return default

    @staticmethod
    def _optional_float(value):
        if value is None or value == "":
            return None
        return float(value)
