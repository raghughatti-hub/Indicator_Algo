from utils.clock import market_time
from datetime import datetime, time, timedelta
from typing import Any

from models.schemas import IntradayRunRequest
from utils.helpers import (
    _strike_mode_from_moneyness,
    _parse_clock,
    _is_market_time,
    _adjust_strike,
    _contract_name,
    _resolved_option_type,
    NSE_OPEN,
)


class DataFeedMixin:
    """Mixin class for IntradayRunner handling technical indicators and broker quote data feeds."""

    def _prefetch_pending_signals(
        self,
        strategy: dict[str, Any],
        request: IntradayRunRequest,
        now: datetime,
    ) -> dict[str, Any]:
        """Phase 1 (no lock): resolve contracts and fetch live quotes for any new signals.

        All broker REST API calls happen here, OUTSIDE order_lock, so the
        _order_watch_loop thread continues updating LTP/PnL without interruption.
        Returns a prefetch_cache dict keyed by signal key.
        """
        cache: dict[str, Any] = {}
        today = now.date()
        start_clock = _parse_clock(request.start_time, NSE_OPEN)
        exit_clock = _parse_clock(request.exit_time, time(15, 15))
        if not _is_market_time(now, start_clock, exit_clock):
            return cache

        mode, steps = _strike_mode_from_moneyness(request.option_moneyness)
        for signal in strategy.get("signals", []):
            signal_time = market_time(signal["time"])
            if signal_time + timedelta(minutes=request.timeframe_minutes) > now:
                continue
            if signal_time.date() != today:
                continue
            if request.trading_mode == "LONG" and signal["side"] != "BUY":
                continue
            if request.trading_mode == "SHORT" and signal["side"] != "SELL":
                continue
            key = f"{signal['time']}|{signal['side']}|{signal['option_type']}"
            if key in self.seen_signal_keys:
                continue
            if self.started_at and signal_time + timedelta(minutes=request.timeframe_minutes) <= market_time(self.started_at):
                continue
            # Already cached this tick
            if key in cache:
                continue
            if request.symbol in {"Spot", "Future"}:
                adjusted_strike = 0
                resolved_opt_type = "CE"
            else:
                resolved_opt_type = _resolved_option_type(signal["side"], request.option_strategy)
                adjusted_strike = _adjust_strike(
                    int(signal.get("strike") or 0),
                    resolved_opt_type,
                    mode,
                    steps,
                    request.strike_interval,
                )
            # Broker REST calls happen here — no lock held
            trade_contract = self._resolve_contract(request, adjusted_strike, resolved_opt_type)
            entry_price, quote_error = self._entry_price(request, signal, trade_contract)
            cache[key] = {
                "adjusted_strike": adjusted_strike,
                "resolved_option_type": resolved_opt_type,
                "trade_contract": trade_contract,
                "entry_price": entry_price,
                "quote_error": quote_error,
            }
        return cache

    def _quote_ltp(self, contract: dict[str, Any] | None) -> tuple[float | None, str | None]:
        """Fetch the current last traded price (LTP) from live broker API."""
        if not contract:
            return None, "No contract resolved."
        if contract.get("error"):
            return None, str(contract["error"])
        token = contract.get("token")
        if not token:
            return None, f"No token for {_contract_name(contract) or 'contract'}."
        try:
            quote = self.zebu.get_quote(contract["exchange"], token)
            value = self.zebu.quote_ltp(quote)
            if value is None:
                return None, f"No LTP in quote for {_contract_name(contract) or token}."
            return value, None
        except Exception as exc:
            return None, str(exc)

    def _quote_snapshot(self, order: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Retrieve full quote snapshot including best bids, best asks, and tick size info."""
        contract = order.get("trade_contract") or {}
        if contract.get("error"):
            return None, str(contract["error"])
        token = contract.get("token")
        if not token:
            return None, f"No token for {_contract_name(contract) or 'contract'}."
        try:
            return self.zebu.quote_snapshot(contract["exchange"], token), None
        except Exception as exc:
            return None, str(exc)

    def _current_order_ltp(self, order: dict[str, Any], fallback: float) -> float | None:
        """Retrieve latest order LTP with standard contract fallback protections."""
        contract = order.get("option_contract") or {}
        price, error = self._quote_ltp(contract)
        if price is not None:
            order["quote_error"] = None
            return price
        order["quote_error"] = error
        return None
