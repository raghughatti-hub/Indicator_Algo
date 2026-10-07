from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import threading
from typing import Any
import uuid

from config.settings import BASE_DIR
from models.schemas import IntradayRunRequest, OrderAdjustRequest
from strategy.engine import run_strategy
from brokers.base import BaseBrokerClient

from utils.helpers import (
    NSE_OPEN,
    NSE_CLOSE,
    INDEX_DATA_SYMBOLS,
    DERIVATIVE_EXCHANGES,
    BSE_UNDERLYINGS,
    OPEN_ORDER_STATUSES,
    ACTIVE_ORDER_STATUSES,
    FINAL_ORDER_STATUSES,
    PRODUCT_TYPE_MAP,
    TERMINAL_STATUS_SECONDS,
    _adjust_strike,
    _strike_mode_from_moneyness,
    _parse_clock,
    _data_symbol,
    _data_exchange,
    _trade_exchange,
    _today_session_start,
    _is_market_time,
    _contract_name,
    _clean_order_id,
    _broker_value,
    _broker_order_id,
    _broker_message,
    _broker_avg_price,
    _broker_filled_qty,
    _flatten_broker_orders,
    _resolved_option_type,
)

from data.tick_processor import DataFeedMixin
from execution.order_manager import OrderManagerMixin
from risk.risk_manager import RiskManagerMixin


# ==============================================================================
# SECTION 3: INTRADAYRUNNER CLASS - DEFINITION & STATE
# ==============================================================================

@dataclass
class IntradayRunner(DataFeedMixin, OrderManagerMixin, RiskManagerMixin):
    broker: BaseBrokerClient | None = None

    @property
    def zebu(self) -> BaseBrokerClient:
        if self.broker is None:
            raise RuntimeError("No broker client is connected. Please connect from the landing page.")
        return self.broker
    lock: threading.RLock = field(default_factory=threading.RLock)
    
    # order_lock guards paper_orders list mutations in the fast-path order watcher
    # and the strategy signal loop independently of the main lock so the two threads
    # never block each other for the expensive candle-fetch + strategy compute work.
    order_lock: threading.RLock = field(default_factory=threading.RLock)
    
    active: bool = False
    request: IntradayRunRequest | None = None
    thread: threading.Thread | None = None
    order_thread: threading.Thread | None = None
    last_error: str | None = None
    last_order_error: str | None = None
    last_update: str | None = None
    last_order_update: str | None = None
    last_skip_reason: str | None = None
    phase: str = "IDLE"
    last_strategy: dict[str, Any] | None = None
    paper_orders: list[dict[str, Any]] = field(default_factory=list)
    seen_signal_keys: set[str] = field(default_factory=set)
    day_pnl: float = 0
    broker_day_pnl: float = 0.0
    started_at: datetime | None = None
    trading_date: str | None = None
    last_terminal_status_at: datetime | None = None
    last_terminal_message: str | None = None
    run_id: str = ""

    # --------------------------------------------------------------------------
    # Life-Cycle Management (Start, Stop, Status Reports)
    # --------------------------------------------------------------------------

    def start(self, request: IntradayRunRequest) -> dict[str, Any]:
        """Start the intraday execution threads."""
        with self.lock:
            if self.active:
                raise RuntimeError("Intraday runner is already active. Stop it before starting a new scrip.")
            if request.live_trade and not self.zebu.settings.live_trading_enabled:
                raise RuntimeError("Real Trade mode is blocked. Set LIVE_TRADING_ENABLED=true before starting live orders.")
            
            request.config.strike_interval = request.strike_interval
            request.paper_trade = not request.live_trade
            self.request = request
            self.active = True
            self.last_error = None
            self.last_order_error = None
            self.phase = "STARTING"
            self.last_terminal_status_at = None
            self.last_terminal_message = None
            self.last_strategy = None
            # Preserve the bar cache across same-day restarts ONLY if trading the same scrip, exchange, and timeframe.
            # Wipe the cache to prevent corrupting indicators by mixing candles from different assets.
            cache_date = getattr(self, "_cached_bars_date", None)
            cache_symbol = getattr(self, "_cached_bars_symbol", None)
            cache_timeframe = getattr(self, "_cached_bars_timeframe", None)
            cache_exchange = getattr(self, "_cached_bars_exchange", None)
            
            today_str = datetime.now().date().isoformat()
            current_symbol = request.underlying
            current_timeframe = request.timeframe_minutes
            current_exchange = request.exchange
            
            if (cache_date != today_str or 
                cache_symbol != current_symbol or 
                cache_timeframe != current_timeframe or 
                cache_exchange != current_exchange):
                self._cached_bars = None
                self._cached_bars_date = today_str
                self._cached_bars_symbol = current_symbol
                self._cached_bars_timeframe = current_timeframe
                self._cached_bars_exchange = current_exchange
            if hasattr(self.zebu, "_quote_cache"):
                self.zebu._quote_cache = {}
                self.zebu._quote_cache_time = {}
            if hasattr(self.zebu, "_token_cache"):
                self.zebu._token_cache = {}
            
            today = datetime.now().date().isoformat()
            if self.trading_date != today:
                self.paper_orders = []
                self.seen_signal_keys = set()
                self.day_pnl = 0
                self.broker_day_pnl = 0.0
                self.trading_date = today
                
            self.started_at = datetime.now()
            self.run_id = str(uuid.uuid4())
            self.thread = threading.Thread(target=self._loop, args=(self.run_id,), daemon=True)
            self.order_thread = threading.Thread(target=self._order_watch_loop, args=(self.run_id,), daemon=True)
            self.thread.start()
            self.order_thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Request the active runner loops to stop execution."""
        with self.lock:
            self.active = False
        return self.status()

    def status(self) -> dict[str, Any]:
        """Return a complete status snapshot of the runner."""
        with self.lock:
            self._normalize_order_rows()
            
            strategy_copy = None
            if self.last_strategy:
                import copy
                strategy_copy = copy.deepcopy(self.last_strategy)
                if self.request:
                    symbol_mode = self.request.symbol
                    signals = strategy_copy.get("signals", [])
                    for s in signals:
                        matching_order = None
                        s_time = s.get("time")
                        s_side = s.get("side")
                        
                        for o in self.paper_orders:
                            if o.get("source_signal_time") == s_time and o.get("source_signal") == s_side:
                                matching_order = o
                                break
                        
                        if matching_order:
                            # Preserve the pure strategy indicator status (OPEN/SL/TP1/TP2/TP3/TSL/OPPOSITE)
                            # and add order_status separately so the Signals block is never polluted
                            # with execution states like Entry_Pending, Active, Closed etc.
                            s["order_status"] = matching_order.get("status")
                            
                            if symbol_mode == "Spot":
                                s["option_type"] = "SPOT"
                                s["strike"] = ""
                            elif symbol_mode == "Future":
                                s["option_type"] = "FUT"
                                s["strike"] = ""
                            else:
                                s["option_type"] = matching_order.get("option_type")
                                s["strike"] = matching_order.get("strike")
                        else:
                            if symbol_mode == "Spot":
                                s["option_type"] = "SPOT"
                                s["strike"] = ""
                            elif symbol_mode == "Future":
                                s["option_type"] = "FUT"
                                s["strike"] = ""
                                
            return {
                "active": self.active,
                "request": self.request.model_dump(mode="json") if self.request else None,
                "last_error": self.last_error,
                "last_order_error": self.last_order_error,
                "last_skip_reason": self.last_skip_reason,
                "last_terminal_message": self.last_terminal_message,
                "last_update": self.last_update,
                "last_order_update": self.last_order_update,
                "phase": self.phase,
                "worker_alive": bool(self.thread and self.thread.is_alive()),
                "order_worker_alive": bool(self.order_thread and self.order_thread.is_alive()),
                "strategy": strategy_copy,
                "paper_orders": list(self.paper_orders),
                "trade_mode": "REAL" if self.request and self.request.live_trade else "PAPER",
                "day_pnl": self.day_pnl,
                "broker_day_pnl": self.broker_day_pnl,
            }

    def orders_status(self) -> dict[str, Any]:
        """Return a lightweight orders-only status dictionary for high-frequency polling."""
        with self.lock:
            self._normalize_order_rows()
            self._log_terminal_status_locked()
            return {
                "active": self.active,
                "paper_orders": list(self.paper_orders),
                "trade_mode": "REAL" if self.request and self.request.live_trade else "PAPER",
                "day_pnl": self.day_pnl,
                "broker_day_pnl": self.broker_day_pnl,
                "last_order_error": self.last_order_error,
                "last_skip_reason": self.last_skip_reason,
                "last_terminal_message": self.last_terminal_message,
                "last_order_update": self.last_order_update,
                "order_worker_alive": bool(self.order_thread and self.order_thread.is_alive()),
            }


    # ==============================================================================
    # SECTION 4: ENGINE LOOPS & TICK PROCESSORS
    # ==============================================================================

    def _loop(self, run_id: str) -> None:
        """Main strategy polling and candle-fetching worker thread loop."""
        while True:
            with self.lock:
                if not self.active or self.run_id != run_id or not self.request:
                    return
                request = self.request
            now = datetime.now()
            exit_time = _parse_clock(request.exit_time, time(15, 15))
            if not request.positional_trade:
                if now.time() > exit_time:
                    # 1. If we are past the exit time cutoff of 15:25 (3:25 PM), stop the runner
                    if now.time() >= time(15, 25):
                        if request.order_product_type == "MIS":
                            # Check if any open orders exist
                            open_order = self._open_paper_order()
                            if open_order:
                                # Trigger exits for orders that are still Active (not Exit_Pending)
                                active_exists = any(o.get("status") == "Active" for o in self.paper_orders)
                                if active_exists:
                                    self._close_open_paper_orders("MIS_EXIT", now)
                                # Wait for real trades to fill before stopping the runner
                            else:
                                with self.lock:
                                    self.active = False
                                    self.phase = "STOPPED_AFTER_EXIT_TIME"
                                    self.last_update = now.isoformat(timespec="seconds")
                                with self.order_lock:
                                    self._log_orders_to_csv()
                                return
                        else:
                            # For NRML: carry forward (do not close), stop runner immediately
                            with self.lock:
                                self.active = False
                                self.phase = "STOPPED_AFTER_EXIT_TIME"
                                self.last_update = now.isoformat(timespec="seconds")
                            with self.order_lock:
                                self._log_orders_to_csv()
                            return
                    else:
                        # 2. Between exit_time and 15:25, keep running to trail active trades,
                        # but if there are no active open orders left, shut down early.
                        open_order = self._open_paper_order()
                        if not open_order:
                            with self.lock:
                                self.active = False
                                self.phase = "STOPPED_AFTER_EXIT_TIME"
                                self.last_update = now.isoformat(timespec="seconds")
                            with self.order_lock:
                                self._log_orders_to_csv()
                            return

            self._tick(request)
            open_order = self._open_paper_order()
            if open_order:
                sleep_time = max(5.0, float(request.poll_seconds))
            else:
                sleep_time = max(2.0, float(request.poll_seconds))
            threading.Event().wait(sleep_time)

    def _order_watch_loop(self, run_id: str) -> None:
        """High-frequency watch loop dedicated to checking active order status and price thresholds."""
        while True:
            with self.lock:
                if not self.active or self.run_id != run_id or not self.request:
                    return
                request = self.request
                strategy = self.last_strategy
            try:
                # order_lock ensures this fast-path loop is the sole writer of
                # paper_orders price/SL/status fields. _tick() no longer calls
                # _update_open_order_prices() so the two threads never collide.
                with self.order_lock:
                    self._update_open_order_prices(request, strategy, allow_exit=True)
                    self._log_orders_to_csv()
                with self.lock:
                    self.last_order_error = None
                    self.last_order_update = datetime.now().isoformat(timespec="seconds")
            except Exception as exc:
                with self.lock:
                    self.last_order_error = str(exc)
                    self.last_order_update = datetime.now().isoformat(timespec="seconds")
            threading.Event().wait(request.order_watch_seconds)

    def _tick(self, request: IntradayRunRequest) -> None:
        """Fetch candles, compile technical strategy calculations, and flag signals."""
        try:
            now = datetime.now()
            start_clock = _parse_clock(request.start_time, NSE_OPEN)
            # Automatically calculate lookback calendar days to ensure we always fetch at least 250 candles
            tf_mins = request.timeframe_minutes
            bars_per_day = 375.0 / max(1, tf_mins)
            required_trading_days = int(250.0 / bars_per_day) + 1
            # Add weekend/non-trading days buffer (approx 1.5x + 2 days)
            required_calendar_days = max(request.lookback_days, int(required_trading_days * 1.5) + 2)
            start = _today_session_start(now, start_clock) - timedelta(days=required_calendar_days)
            with self.lock:
                self.phase = "FETCHING_BROKER_CANDLES"
                self.last_update = datetime.now().isoformat(timespec="seconds")
            
            # get_bars() is a network call that can take several seconds.
            # It runs without holding any lock so the order watcher continues
            # updating prices at full speed during candle fetch.
            if getattr(self, "_cached_bars", None) is not None and len(self._cached_bars) >= 220:
                # Cache is warm — only fetch the last 2 hours to pick up new candles.
                # This keeps each tick fast (<1s) instead of refetching 2 days of data.
                fetch_start = now - timedelta(hours=2)
            else:
                fetch_start = start

            contract_exchange = _data_exchange(request.exchange, request.underlying)
            contract_symbol = _data_symbol(request.underlying)
            future_token = None
            
            if request.exchange in {"NFO", "CDS", "MCX", "BFO"} and request.underlying.strip().upper() not in {"SENSEX", "BANKEX"}:
                try:
                    future_contract = self.zebu.resolve_future(request.underlying, request.exchange, request.option_expiry)
                    if future_contract:
                        contract_exchange = request.exchange
                        contract_symbol = future_contract["tradingsymbol"]
                        future_token = future_contract.get("token")
                except Exception as exc:
                    print(f"Error resolving underlying future contract: {exc}", flush=True)

            fetched_bars = self.zebu.get_bars(
                exchange=contract_exchange,
                symbol=contract_symbol,
                interval=request.timeframe_minutes,
                start=fetch_start,
                end=now,
            )

            if fetched_bars:
                if getattr(self, "_cached_bars", None) is not None:
                    merged = {b["time"]: b for b in self._cached_bars}
                    for b in fetched_bars:
                        merged[b["time"]] = b
                    sorted_keys = sorted(merged.keys())
                    self._cached_bars = [merged[k] for k in sorted_keys[-3000:]]
                else:
                    self._cached_bars = fetched_bars

            bars = [dict(b) for b in (getattr(self, "_cached_bars", None) or [])]
            if not bars:
                raise RuntimeError(f"No candle data returned from broker for {contract_symbol}. Check broker session or market hours.")
            # Inject the real-time live LTP of the underlying into the bars list
            # so the parent strategy indicators and Signals block P&L update in real time.
            try:
                underlying_ex = contract_exchange
                underlying_sym = contract_symbol
                if underlying_ex.upper() == "INDICES" or underlying_sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]:
                    idx_quote = self.zebu.get_single_index_quote(underlying_sym)
                    live_ltp = idx_quote["ltp"] if idx_quote else None
                else:
                    broker_exchange = "NSE" if underlying_ex.upper() == "INDICES" else underlying_ex
                    search_ex = "INDICES" if underlying_ex.upper() == "INDICES" else broker_exchange
                    token = future_token if (underlying_ex == request.exchange and future_token) else self.zebu.search_token(search_ex, underlying_sym)
                    if token:
                        quote = self.zebu.get_quote(broker_exchange, token)
                        live_ltp = self.zebu.quote_ltp(quote)
                    if live_ltp is not None:
                        T = request.timeframe_minutes
                        candle_start = now.replace(minute=(now.minute // T) * T, second=0, microsecond=0)
                        if bars:
                            last_bar = bars[-1]
                            last_time = last_bar["time"]
                            if isinstance(last_time, str):
                                last_time = datetime.fromisoformat(last_time)
                            elif hasattr(last_time, "to_pydatetime"):
                                last_time = last_time.to_pydatetime()
                            
                            last_time_naive = last_time.replace(tzinfo=None)
                            candle_start_naive = candle_start.replace(tzinfo=None)
                            
                            if last_time_naive == candle_start_naive:
                                last_bar["close"] = live_ltp
                                last_bar["high"] = max(last_bar["high"], live_ltp)
                                last_bar["low"] = min(last_bar["low"], live_ltp)
                            elif last_time_naive < candle_start_naive:
                                new_bar = {
                                    "time": candle_start_naive,
                                    "open": last_bar["close"],
                                    "high": max(last_bar["close"], live_ltp),
                                    "low": min(last_bar["close"], live_ltp),
                                    "close": live_ltp,
                                    "volume": 0
                                }
                                bars.append(new_bar)
            except Exception as e:
                # Silently ignore known transient broker rate limits or quote fetch failures to keep console clean
                err_msg = str(e)
                if "Quote fetch failed" not in err_msg and "rate limit" not in err_msg.lower():
                    print(f"Error fetching/injecting live LTP for underlying: {e}", flush=True)

            with self.lock:
                self.phase = "RUNNING_STRATEGY"
                self.last_update = datetime.now().isoformat(timespec="seconds")
            strategy = run_strategy(bars, request.config)
            
            if request.paper_trade or request.live_trade:
                with self.lock:
                    self.phase = "UPDATING_PAPER_TRADES"
                    self.last_update = datetime.now().isoformat(timespec="seconds")
                # ------------------------------------------------------------------
                # TWO-PHASE SIGNAL ENTRY to keep order_lock free during broker calls:
                #
                # Phase 1 (NO lock): Resolve contracts, fetch live quotes from broker.
                #          Broker REST calls can take 2-8s — we must NOT hold order_lock
                #          here or the _order_watch_loop will freeze and LTP stops updating.
                #
                # Phase 2 (WITH lock): Commit resolved order data to paper_orders list.
                #          This critical section is fast (pure in-memory work, <1ms).
                # ------------------------------------------------------------------
                prefetch = self._prefetch_pending_signals(strategy, request, now)
                with self.order_lock:
                    self._paper_trade_new_today_signals(strategy, request, now, prefetch_cache=prefetch)
                    self._log_orders_to_csv()
            with self.lock:
                self.last_strategy = strategy
                self.last_update = datetime.now().isoformat(timespec="seconds")
                self.phase = "WAITING_FOR_NEXT_POLL"
                self.last_error = None
                # Print terminal status on every tick so the console stays live
                # even when the browser UI tab is idle/sleeping.
                self._log_terminal_status_locked(force=True)
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
                self.phase = "ERROR"
                self.last_update = datetime.now().isoformat(timespec="seconds")
                self._log_terminal_status_locked(force=True)


# ==============================================================================
# SECTION 5: SIGNAL EVALUATION & ENTRY ORDER PLACEMENT
# ==============================================================================


    def _paper_trade_new_today_signals(
        self,
        strategy: dict[str, Any],
        request: IntradayRunRequest,
        now: datetime,
        prefetch_cache: dict[str, Any] | None = None,
    ) -> None:
        """Phase 2 (order_lock held): commit new signals to paper_orders using pre-fetched data.

        Broker API work was already done in _prefetch_pending_signals() before
        this function is called, so the lock is held for <1 ms (pure memory ops).
        """
        today = now.date()
        start_clock = _parse_clock(request.start_time, NSE_OPEN)
        exit_clock = _parse_clock(request.exit_time, time(15, 15))
        market_open = _is_market_time(now, start_clock, exit_clock)

        if not market_open:
            self.last_skip_reason = "Signal ignored because current time is outside Start Time / Exit Time."
            return
        if self.day_pnl >= request.max_profit or self.day_pnl <= request.max_loss:
            self.last_skip_reason = f"Signal ignored because day P&L {self.day_pnl:.2f} reached Max Profit/Loss limits."
            return

        mode, steps = _strike_mode_from_moneyness(request.option_moneyness)
        cache = prefetch_cache or {}

        for signal in sorted(strategy.get("signals", []), key=lambda item: item["time"], reverse=True):
            signal_time = datetime.fromisoformat(signal["time"])
            if signal_time.date() != today:
                continue
            if request.trading_mode == "LONG" and signal["side"] != "BUY":
                continue
            if request.trading_mode == "SHORT" and signal["side"] != "SELL":
                continue

            key = f"{signal['time']}|{signal['side']}|{signal['option_type']}"
            if key in self.seen_signal_keys:
                continue
            if self.started_at and signal_time < self.started_at:
                self.seen_signal_keys.add(key)
                continue

            open_orders = [o for o in self.paper_orders if o.get("status") in OPEN_ORDER_STATUSES]
            valid_trades_count = sum(
                1 for o in self.paper_orders
                if o.get("status") not in {"Entry_Rejected", "QUOTE_ERROR"}
            )
            if valid_trades_count >= request.max_trades_per_day and not open_orders:
                self.last_skip_reason = f"Signal ignored because Max Trades Per Day is {request.max_trades_per_day}."
                break

            closed_orders = []
            skip_signal = False
            for o_ord in list(open_orders):
                if o_ord.get("status") != "Active":
                    self.last_skip_reason = f"Signal ignored because an order is already {o_ord.get('status')}."
                    skip_signal = True
                    break
                if o_ord.get("source_signal") == signal["side"]:
                    self.last_skip_reason = "Signal ignored because same-side order is already running."
                    skip_signal = True
                    break
                if self._close_order(o_ord, "OPPOSITE_SIGNAL", now):
                    closed_orders.append(o_ord)
                else:
                    self.last_skip_reason = "Opposite signal could not close the current order."
                    skip_signal = True
                    break

            if skip_signal:
                continue

            # Use pre-fetched contract/quote data (resolved outside the lock).
            # Fall back to inline resolution only when cache is missing (e.g. first tick).
            cached = cache.get(key)
            if cached:
                adjusted_strike = cached["adjusted_strike"]
                resolved_option_type = cached.get("resolved_option_type", "CE")
                trade_contract = cached["trade_contract"]
                entry_price = cached["entry_price"]
                quote_error = cached["quote_error"]
            else:
                if request.symbol in {"Spot", "Future"}:
                    adjusted_strike = 0
                    resolved_option_type = "CE"
                else:
                    resolved_option_type = _resolved_option_type(signal["side"], request.option_strategy)
                    adjusted_strike = _adjust_strike(
                        int(signal.get("strike") or 0),
                        resolved_option_type,
                        mode,
                        steps,
                        request.strike_interval,
                    )
                trade_contract = self._resolve_contract(request, adjusted_strike, resolved_option_type)
                entry_price, quote_error = self._entry_price(request, signal, trade_contract)

            entry_ready = entry_price is not None

            if entry_ready and self._entry_waits_for_blank_limit(request):
                self.last_skip_reason = "Conditional entry is waiting because Entry Limit Price is blank."
                continue

            sl_price = self._sl_price(entry_price, request) if entry_ready else None
            target_price = self._target_price(entry_price, request) if entry_ready else None

            order = {
                "order_key": str(uuid.uuid4()),
                "time": signal["time"],
                "entry_time": signal["time"],
                "scripname": request.underlying,
                "underlying": request.underlying,
                "instrument_type": request.symbol,
                "side": request.option_strategy,
                "paper": True,
                "live_trade": request.live_trade,
                "market_open": market_open,
                "quantity": request.qty,
                "option_type": resolved_option_type if request.symbol == "Option" else ("FUT" if request.symbol == "Future" else "SPOT"),
                "strike_mode": mode,
                "strike": adjusted_strike,
                "trade_contract": trade_contract,
                "option_contract": trade_contract,
                "tradingsymbol": _contract_name(trade_contract),
                "tick_size": request.tick_size or (trade_contract or {}).get("tick_size") or (trade_contract or {}).get("raw", {}).get("TickSize") or 0.05,
                "option_entry": entry_price,
                "option_ltp": entry_price,
                "option_exit": None,
                "exit_time": None,
                "initial_stoploss": sl_price,
                "stoploss": sl_price,
                "target": target_price,
                "spot_entry": signal["entry"],
                "status": "Idle",
                "pnl": 0,
                "quote_error": quote_error,
                "quote_retry_count": 0,
                "source_signal": signal["side"],
                "source_signal_time": signal["time"],
                "source_signal_entry": signal["entry"],
                "trail_anchor": entry_price,
                "trail_active": False,
                "entry_order_id": None,
                "entry_order_price": None,
                "entry_remarks": quote_error if not entry_ready else None,
                "entry_order_response": None,
                "exit_order_id": None,
                "exit_order_price": None,
                "exit_remarks": None,
                "exit_order_response": None,
                "broker_entry_status": None,
                "broker_exit_status": None,
                "pending_exit_reason": None,
                "last_exit_error": None,
                "trailing_stoploss": request.trailing_stoploss,
                "trail_start_type": request.trail_start_type,
                "trail_start_value": request.trail_start_value,
                "trail_when_moves_by": request.trail_when_moves_by,
                "trail_move_sl_by": request.trail_move_sl_by,
                "move_sl_to_cost": request.move_sl_to_cost,
                "move_sl_to_cost_points": request.move_sl_to_cost_points,
                "sl_moved_to_cost": False,
            }

            # Guard: only append if no open order exists (excluding any we just closed).
            closed_keys = {c.get("order_key") for c in closed_orders}
            has_other_open = False
            for o in self.paper_orders:
                if o.get("status") in OPEN_ORDER_STATUSES:
                    if o.get("order_key") in closed_keys:
                        continue
                    has_other_open = True
                    break

            if has_other_open:
                self.last_skip_reason = "New signal ignored because another order became active/pending first."
                continue

            self.paper_orders.append(order)

            # Mark as seen ONLY after the order row is successfully appended.
            self.seen_signal_keys.add(key)
            self.last_skip_reason = None

            if entry_ready:
                self._process_entry_order(order, request)
            break

    def _open_paper_order(self) -> dict[str, Any] | None:
        """Find the latest open (Idle/Pending/Active) order row in the local database list."""
        for order in reversed(self.paper_orders):
            if order.get("status") in OPEN_ORDER_STATUSES:
                return order
        return None

    def _resolve_contract(self, request: IntradayRunRequest, strike: int, option_type: str) -> dict[str, Any] | None:
        """Resolve specific instrument metadata for derivatives from Zebu's cached Master."""
        try:
            te = _trade_exchange(request.exchange, request.symbol, request.underlying)
            if request.symbol == "Spot":
                return self.zebu.resolve_spot(te, request.underlying)
            if request.symbol == "Future":
                return self.zebu.resolve_future(request.underlying, te, request.option_expiry)
            return self.zebu.resolve_option(request.underlying, strike, option_type, request.option_expiry, te)
        except Exception as exc:
            return {
                "error": str(exc),
                "exchange": _trade_exchange(request.exchange, request.symbol, request.underlying),
                "tradingsymbol": f"{request.underlying} {request.symbol} {strike} {option_type}",
                "token": None,
            }

    def _entry_price(self, request: IntradayRunRequest, signal: dict[str, Any], contract: dict[str, Any] | None) -> tuple[float | None, str | None]:
        """Fetch option entry LTP quote from Zebu, falling back to spot signal price if cash/future."""
        price, error = self._quote_ltp(contract)
        if price is not None:
            return price, None
        if request.symbol == "Option":
            return None, error or "No live option quote available for selected contract."
        return float(signal["entry"]), error




# ==============================================================================
# SECTION 6: HIGH-FREQUENCY ORDER PROCESSOR & PRICE UPDATER
# ==============================================================================

    def _update_open_order_prices(self, request: IntradayRunRequest, strategy: dict[str, Any] | None = None, allow_exit: bool = True) -> None:
        """Poll quotes for active orders, calculate MTM, adjust trailing stops, and handle limits/stops."""
        strategy = strategy or {}
        last_close = float(strategy.get("summary", {}).get("last_close") or 0)
        total = 0.0
        
        # Throttled broker position sync
        now_ts = datetime.now().timestamp()
        if request.live_trade and (not hasattr(self, "_last_positions_sync_at") or (now_ts - self._last_positions_sync_at) >= 3.0):
            self._last_positions_sync_at = now_ts
            self._sync_broker_positions(request)
            
        for order in self.paper_orders:
            self._sync_broker_order_state(order)
            if order["status"] == "Idle":
                self._process_entry_order(order, request)
                
            if order["status"] not in ACTIVE_ORDER_STATUSES:
                total += float(order.get("pnl") or 0)
                continue
                
            ltp = self._current_order_ltp(order, last_close)
            if ltp is None:
                order["quote_error"] = "Live option quote unavailable while updating order."
                continue
                
            order["option_ltp"] = ltp
            
            # Trigger any pending manual limit orders
            if order.get("pending_manual_orders"):
                remaining_pending = []
                for p_order in order["pending_manual_orders"]:
                    p_action = p_order["action"]
                    p_qty = p_order["quantity"]
                    p_price = p_order["price"]
                    p_id = p_order.get("order_id")
                    
                    triggered = False
                    fill_price = p_price
                    
                    if p_id: # Live trade pending manual order
                        try:
                            # Synchronize status from broker
                            broker = self._broker_order_status(str(p_id))
                            state = self._mapped_broker_state(broker)
                            if state == "filled":
                                triggered = True
                                fill_price = _broker_avg_price(broker) or p_price
                            elif state == "rejected" or state == "cancelled":
                                # Discard it from pending queue (don't add to remaining)
                                continue
                        except Exception as e:
                            # Log error and retry next tick
                            print(f"[AutomationRunner] Error syncing pending manual order {p_id}: {e}", flush=True)
                            remaining_pending.append(p_order)
                            continue
                    else: # Paper mode pending manual order
                        if p_action == "BUY" and ltp <= p_price:
                            triggered = True
                        elif p_action == "SELL" and ltp >= p_price:
                            triggered = True
                    
                    if triggered:
                        self._apply_manual_trade_fill(order, p_action, p_qty, fill_price)
                        order["adjusted_at"] = datetime.now().isoformat(timespec="seconds")
                        self.last_order_update = order["adjusted_at"]
                    else:
                        remaining_pending.append(p_order)
                
                order["pending_manual_orders"] = remaining_pending
                
                # Check if position became closed as a result of manual fills
                if order["status"] not in ACTIVE_ORDER_STATUSES:
                    total += float(order.get("pnl") or 0)
                    continue
            
            direction = 1 if order["side"] == "BUY" else -1
            pnl = (ltp - order["option_entry"]) * direction * order["quantity"] + order.get("realized_pnl", 0.0)
            order["pnl"] = pnl
            total += pnl
            
            order_trailing = order.get("trailing_stoploss") if order.get("trailing_stoploss") is not None else request.trailing_stoploss
            order_move_to_cost = order.get("move_sl_to_cost") if order.get("move_sl_to_cost") is not None else request.move_sl_to_cost
            if order_trailing or order_move_to_cost:
                self._update_trailing_stop(order, ltp, request, direction)

            # Check if a newer opposite strategy signal has arrived after this order's entry
            latest_signal = strategy.get("signals", [])[-1] if strategy.get("signals") else None
            if latest_signal and allow_exit and order["status"] == "Active":
                if str(latest_signal.get("side")) != str(order.get("source_signal")):
                    sig_time_str = str(latest_signal.get("time"))
                    ord_time_str = str(order.get("source_signal_time") or order.get("entry_time"))
                    if sig_time_str > ord_time_str:
                        self._close_order(order, "OPPOSITE_SIGNAL", datetime.now(), ltp)
                        continue
                
            strategy_exit = self._sync_strategy_sltp(order, request, strategy)
            if strategy_exit and allow_exit and order["status"] == "Active":
                self._close_order(order, strategy_exit, datetime.now(), ltp)
                continue
                
            if request.sltp_instrument == "strategy":
                # Check manual option-level SL and Target overrides even under strategy mode
                hit_sl = False
                hit_target = False
                if order.get("manual_stoploss") and order.get("stoploss") is not None:
                    hit_sl = ltp <= order["stoploss"] if order["side"] == "BUY" else ltp >= order["stoploss"]
                if order.get("manual_target") and order.get("target") is not None:
                    hit_target = ltp >= order["target"] if order["side"] == "BUY" else ltp <= order["target"]
                if allow_exit and order["status"] == "Active":
                    if hit_sl:
                        is_trailed = bool(order.get("trail_active") or order.get("sl_moved_to_cost") or (order.get("initial_stoploss") is not None and order.get("stoploss") != order.get("initial_stoploss")))
                        exit_reason = "TSL" if is_trailed else "SL"
                        self._close_order(order, exit_reason, datetime.now(), ltp)
                        continue
                    elif hit_target:
                        self._close_order(order, "TARGET", datetime.now(), ltp)
                        continue
                continue
                
            hit_sl = ltp <= order["stoploss"] if order["side"] == "BUY" else ltp >= order["stoploss"]
            hit_target = ltp >= order["target"] if order["side"] == "BUY" else ltp <= order["target"]
            if allow_exit and order["status"] == "Active":
                if hit_sl:
                    is_trailed = bool(order.get("trail_active") or order.get("sl_moved_to_cost") or (order.get("initial_stoploss") is not None and order.get("stoploss") != order.get("initial_stoploss")))
                    exit_reason = "TSL" if is_trailed else "SL"
                    self._close_order(order, exit_reason, datetime.now(), ltp)
                elif hit_target:
                    self._close_order(order, "TARGET", datetime.now(), ltp)
        self.day_pnl = total

    def _sync_broker_positions(self, request: IntradayRunRequest) -> None:
        """Fetch positions from broker and synchronize quantities, entry prices, PnL, and import manual trades."""
        if not self.broker or not self.zebu.connected:
            return
            
        try:
            positions = self.zebu.get_positions()
        except Exception as e:
            print(f"[BrokerSync] Error fetching broker positions: {e}", flush=True)
            return
            
        try:
            if not positions:
                positions = []
            elif isinstance(positions, dict):
                if positions.get("stat") == "Not_Ok":
                    return
                positions = [positions]
                
            flat_positions = []
            if isinstance(positions, list):
                for pos in positions:
                    if isinstance(pos, dict) and pos.get("stat") != "Not_Ok":
                        flat_positions.append(pos)
                        
            active_orders_map = {}
            for order in self.paper_orders:
                if order.get("status") in ACTIVE_ORDER_STATUSES and order.get("live_trade"):
                    active_orders_map[order["tradingsymbol"]] = order
                        
            matched_tsyms = set()
            underlying_prefix = request.underlying.upper().strip()
            
            total_broker_mtm = 0.0
            
            for pos in flat_positions:
                tsym = pos.get("tsym")
                if not tsym:
                    continue
                    
                netqty = int(float(pos.get("netqty", 0)))
                
                # Check underlying prefix match
                if underlying_prefix == "NIFTY" and tsym.upper().startswith("NIFTYNXT"):
                    continue
                if not tsym.upper().startswith(underlying_prefix):
                    continue
                    
                urmtom = float(pos.get("urmtom") or 0.0)
                rpnl = float(pos.get("rpnl") or 0.0)
                total_broker_mtm += (urmtom + rpnl)
                
                matched_tsyms.add(tsym)
                
                if tsym in active_orders_map:
                    order = active_orders_map[tsym]
                    order["broker_net_qty"] = abs(netqty)
                            
            self.broker_day_pnl = total_broker_mtm
            
            # Check for active tracked orders that are absent at broker (closed/not created)
            for tsym, order in active_orders_map.items():
                if tsym not in matched_tsyms:
                    order["broker_net_qty"] = 0
                        
        except Exception as e:
            print(f"[BrokerSync] Error synchronizing broker positions: {e}", flush=True)


# ==============================================================================
# SECTION 10: USER-FACING CONTROL PANEL & ADJUSTMENT ACTIONS
# ==============================================================================

    def adjust_order(self, request: OrderAdjustRequest) -> dict[str, Any]:
        """User action handler: manually adjust SL, Target, or Trailing parameters for an active order."""
        with self.lock:
            order = self._find_order(request.order_key)
            if not order:
                raise RuntimeError("Order row not found.")
            if request.stoploss is not None:
                order["stoploss"] = float(request.stoploss)
                order["manual_stoploss"] = True
            if request.target is not None:
                order["target"] = float(request.target)
                order["manual_target"] = True
            if request.trail_start_value is not None:
                order["trail_start_value"] = float(request.trail_start_value)
            if request.trailing_stoploss is not None:
                order["trailing_stoploss"] = bool(request.trailing_stoploss)
            if request.trail_when_moves_by is not None:
                order["trail_when_moves_by"] = float(request.trail_when_moves_by)
            if request.trail_move_sl_by is not None:
                order["trail_move_sl_by"] = float(request.trail_move_sl_by)
            if request.move_sl_to_cost is not None:
                if bool(request.move_sl_to_cost):
                    order["move_sl_to_cost"] = True
                    if not order.get("sl_moved_to_cost", False):
                        direction = 1 if order["side"] == "BUY" else -1
                        cost_sl = float(order["option_entry"])
                        current_sl = float(order["stoploss"])
                        if direction == 1:
                            if current_sl < cost_sl:
                                order["stoploss"] = cost_sl
                        else:
                            if current_sl > cost_sl:
                                order["stoploss"] = cost_sl
                        order["sl_moved_to_cost"] = True
                else:
                    if self.request:
                        order["move_sl_to_cost"] = self.request.move_sl_to_cost
                        order["move_sl_to_cost_points"] = self.request.move_sl_to_cost_points
                    else:
                        order["move_sl_to_cost"] = False
            if request.move_sl_to_cost_points is not None:
                order["move_sl_to_cost_points"] = float(request.move_sl_to_cost_points)
                
            order["entry_remarks"] = order.get("entry_remarks") or "Manual controls updated"
            order["adjusted_at"] = datetime.now().isoformat(timespec="seconds")
            self.last_order_update = order["adjusted_at"]
        return self.orders_status()

    def exit_open_order(self) -> dict[str, Any]:
        """User action handler: manually trigger immediate exit closure for any active trade."""
        with self.lock:
            order = self._open_paper_order()
        if not order:
            with self.lock:
                self.last_error = None
            return self.status()
            
        closed = self._close_order(order, "MANUAL_EXIT", datetime.now())
        with self.lock:
            if not closed:
                self.last_error = "Manual exit failed. Check quote availability or live exit order response."
            self.last_update = datetime.now().isoformat(timespec="seconds")
        return self.status()

    def manual_trade_action(self, order_key: str, action: str, quantity: int, price_str: str) -> dict[str, Any]:
        """Place manual BUY or SELL order for the active trade scrip, adjusting quantity and cost average."""
        import time
        
        with self.lock:
            order = self._find_order(order_key)
            if not order:
                raise RuntimeError("No active order found matching the key.")
            if order["status"] not in ACTIVE_ORDER_STATUSES:
                raise RuntimeError(f"Order is not active (current status: {order['status']}).")
            
            contract = order.get("option_contract")
            if not contract or contract.get("error"):
                raise RuntimeError("No active option contract resolved.")
            
            # Extract variables needed for quote snapshot fetching and checks outside the lock
            exchange = contract.get("exchange")
            token = contract.get("token")
            symbol_name = _contract_name(contract)
            order_side = order["side"]
            order_tick_size = order.get("tick_size")
            order_option_ltp = order.get("option_ltp")
            live_trade = order.get("live_trade", False)
            
            # Determine limit price based on market or manual price
            price_str_clean = price_str.strip().lower()
            is_market = price_str_clean in {"at mkt", "market", "", "mkt"}
            
            tradingsymbol = order.get("tradingsymbol") or ""
            product_type = PRODUCT_TYPE_MAP.get(self.request.order_product_type if self.request else "MIS", "I")

        if not token:
            raise RuntimeError(f"No token for {symbol_name or 'contract'}.")

        # Retrieve quote outside the lock to prevent blocking watcher loop on API delays
        quote = None
        error = None
        try:
            quote = self.zebu.quote_snapshot(exchange, token)
        except Exception as exc:
            error = str(exc)
            # Try to fall back to WebSocket cache if REST API fails or times out
            try:
                ws_key = (exchange.upper(), str(token))
                if self.zebu._ws_feed_opened and ws_key in self.zebu._ws_quotes:
                    ws_quote = self.zebu._ws_quotes[ws_key]
                    ltp = self.zebu.quote_ltp(ws_quote)
                    best_buy = self.zebu._quote_number(ws_quote, ("bp1", "best_buy", "best_bid", "bid", "b1", "bp"))
                    best_sell = self.zebu._quote_number(ws_quote, ("sp1", "best_sell", "best_ask", "ask", "a1", "sp"))
                    tick_size = self.zebu._quote_number(ws_quote, ("ti", "tick_size", "ticksize", "tick", "pp"))
                    quote = {
                        "raw": ws_quote,
                        "ltp": ltp,
                        "best_buy": best_buy,
                        "best_sell": best_sell,
                        "tick_size": tick_size or 0.05,
                    }
                    print(f"[manual_trade_action] Zebu REST API failed ({error}). Successfully fell back to WebSocket quote cache.", flush=True)
            except Exception as ws_err:
                print(f"[manual_trade_action] WebSocket cache fallback failed: {ws_err}", flush=True)

        if not quote and is_market:
            raise RuntimeError(error or f"Could not fetch quote snapshot for market price (Symbol: {symbol_name}, Token: {token}).")
        
        tick = float((quote or {}).get("tick_size") or order_tick_size or 0.05)
        ltp = (quote or {}).get("ltp") or order_option_ltp
        
        if is_market:
            # Same logic as aggressive entry/exit in Order Management
            if action == order_side:
                raw = (quote.get("best_buy") if action == "BUY" else quote.get("best_sell")) or ltp
                if raw is None:
                    raise RuntimeError("No quote price available for market entry.")
                raw = raw + tick if action == "BUY" else raw - tick
                price = self._round_to_tick(float(raw), tick, action)
            else:
                raw = quote.get("best_buy") if action == "SELL" else quote.get("best_sell")
                raw = raw if raw is not None else ltp
                if raw is None:
                    raise RuntimeError("No quote price available for market exit.")
                raw = raw + tick if action == "SELL" else raw - tick
                price = self._round_to_tick(float(raw), tick, action)
        else:
            try:
                price = self._round_to_tick(float(price_str), tick, action)
            except ValueError:
                raise RuntimeError(f"Invalid manual price format: {price_str}")
        
        if not live_trade:
            # Paper Mode: queue as a pending limit order or fill immediately if marketable
            with self.lock:
                order = self._find_order(order_key)
                if not order or order["status"] not in ACTIVE_ORDER_STATUSES:
                    raise RuntimeError("Order became inactive while fetching quote.")
                
                # Retrieve current option ltp or quote ltp
                current_ltp = float(order.get("option_ltp") or ltp or 0.0)
                
                # Check trigger condition for pending limit order
                is_pending = False
                if not is_market:
                    if action == "BUY" and price < current_ltp:
                        is_pending = True
                    elif action == "SELL" and price > current_ltp:
                        is_pending = True
                
                if is_pending:
                    # Queue it as a pending manual order
                    if "pending_manual_orders" not in order:
                        order["pending_manual_orders"] = []
                    order["pending_manual_orders"].append({
                        "action": action,
                        "quantity": int(quantity),
                        "price": price,
                        "created_at": datetime.now().isoformat(timespec="seconds")
                    })
                    # Set temporary remarks to inform the user
                    order["entry_remarks"] = f"Pending Manual {action} Limit @ {price:.2f} (Qty: {quantity})"
                    order["adjusted_at"] = datetime.now().isoformat(timespec="seconds")
                    self.last_order_update = order["adjusted_at"]
                else:
                    # Fill immediately
                    self._apply_manual_trade_fill(order, action, quantity, price)
                    order["adjusted_at"] = datetime.now().isoformat(timespec="seconds")
                    self.last_order_update = order["adjusted_at"]
                    
            with self.order_lock:
                self._log_orders_to_csv()
            return self.orders_status()

        # Outside the lock for Zebu REST calls to prevent blocking the watcher loop
        try:
            response = self.zebu.place_order(
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                side=action,
                quantity=int(quantity),
                product_type=product_type,
                price_type="LMT",
                price=price,
                trigger_price=0,
                confirm_live=True,
            )
            if response.get("paper"):
                raise RuntimeError(response.get("message", "Live order blocked."))
            if response.get("stat") == "Not_Ok":
                raise RuntimeError(response.get("emsg", "Broker rejected order."))
            order_id = self._order_id(response)
            if not order_id:
                raise RuntimeError("No order ID returned by broker.")
        except Exception as exc:
            raise RuntimeError(f"Live order placement failed: {exc}")
        
        # Poll the broker to check if filled
        filled = False
        fill_price = price
        for _ in range(6): # 6 * 500ms = 3s
            time.sleep(0.5)
            broker_status = self._broker_order_status(str(order_id))
            state = self._mapped_broker_state(broker_status)
            if state == "filled":
                filled = True
                fill_price = _broker_avg_price(broker_status) or price
                break
            elif state == "rejected":
                reason = _broker_message(broker_status) or "Broker rejected the order"
                raise RuntimeError(f"Broker rejected: {reason}")
        
        if not filled:
            # Queue as a pending manual order for live broker polling in background
            with self.lock:
                order = self._find_order(order_key)
                if not order or order["status"] not in ACTIVE_ORDER_STATUSES:
                    raise RuntimeError("Order became inactive while waiting for fill.")
                
                if "pending_manual_orders" not in order:
                    order["pending_manual_orders"] = []
                order["pending_manual_orders"].append({
                    "action": action,
                    "quantity": int(quantity),
                    "price": price,
                    "order_id": str(order_id),
                    "created_at": datetime.now().isoformat(timespec="seconds")
                })
                # Set temporary remarks to inform the user
                order["entry_remarks"] = f"Pending Manual {action} Limit @ {price:.2f} (Qty: {quantity})"
                order["adjusted_at"] = datetime.now().isoformat(timespec="seconds")
                self.last_order_update = order["adjusted_at"]
            with self.order_lock:
                self._log_orders_to_csv()
            return self.orders_status()
        
        with self.lock:
            order = self._find_order(order_key)
            if not order or order["status"] not in ACTIVE_ORDER_STATUSES:
                raise RuntimeError("Order became inactive while waiting for fill.")
            self._apply_manual_trade_fill(order, action, quantity, fill_price)
            order["adjusted_at"] = datetime.now().isoformat(timespec="seconds")
            self.last_order_update = order["adjusted_at"]
        with self.order_lock:
            self._log_orders_to_csv()
        return self.orders_status()

    def cancel_manual_trade_action(self, order_key: str) -> dict[str, Any]:
        """Cancel any pending manual limit orders for the active trade."""
        with self.lock:
            order = self._find_order(order_key)
            if not order:
                raise RuntimeError("No active order found matching the key.")
            
            pending_orders = order.get("pending_manual_orders", [])
            if not pending_orders:
                return self.orders_status()
            
            # If it is a live trade, cancel open orders at the broker
            if order.get("live_trade"):
                for p_order in pending_orders:
                    p_id = p_order.get("order_id")
                    if p_id:
                        try:
                            self.zebu.cancel_order(str(p_id))
                        except Exception as e:
                            print(f"[AutomationRunner] Error canceling broker order {p_id}: {e}", flush=True)
            
            # Clear pending manual orders list
            order["pending_manual_orders"] = []
            
            # Reset remarks to original active state remarks
            if order.get("status") == "Active":
                order["entry_remarks"] = "Paper entry active" if not order.get("live_trade") else (order.get("entry_remarks") or "")
            
            order["adjusted_at"] = datetime.now().isoformat(timespec="seconds")
            self.last_order_update = order["adjusted_at"]
            
        with self.order_lock:
            self._log_orders_to_csv()
        return self.orders_status()


    def _apply_manual_trade_fill(self, order: dict[str, Any], action: str, quantity: int, price: float) -> None:
        """Apply a manual buy/sell fill to the active order, adjusting quantity and cost average."""
        old_qty = int(order["quantity"])
        old_entry = float(order["option_entry"])
        direction = 1 if order["side"] == "BUY" else -1
        
        if action == order["side"]:
            # Adding to position
            new_qty = old_qty + quantity
            new_entry = ((old_qty * old_entry) + (quantity * price)) / new_qty
            order["quantity"] = new_qty
            order["option_entry"] = new_entry
            
            # Recalculate SL and target if they are not manual
            if self.request:
                if not order.get("manual_stoploss"):
                    sl_val = self._sl_price(new_entry, self.request)
                    order["initial_stoploss"] = sl_val
                    order["stoploss"] = sl_val
                if not order.get("manual_target"):
                    order["target"] = self._target_price(new_entry, self.request)
            
            order["entry_remarks"] = f"Added {quantity} qty @ {price:.2f}"
        else:
            # Reducing position
            trade_qty = min(quantity, old_qty)
            new_qty = old_qty - trade_qty
            
            # Realize the P&L on the exited portion
            realized = (price - old_entry) * direction * trade_qty
            order["realized_pnl"] = order.get("realized_pnl", 0.0) + realized
            
            if new_qty == 0:
                # Position is fully closed
                self._mark_order_closed(order, "MANUAL_EXIT", datetime.now(), price, remaining_qty=0)
                order["exit_remarks"] = f"Fully closed via manual Sell @ {price:.2f}"
            else:
                order["quantity"] = new_qty
                # Update current active P&L
                ltp = float(order.get("option_ltp") or price)
                order["pnl"] = (ltp - old_entry) * direction * new_qty + order.get("realized_pnl", 0.0)
                order["entry_remarks"] = f"Exited {trade_qty} qty @ {price:.2f}"


# ==============================================================================
# SECTION 11: WORKER STATE NORMALIZATION & DIAGNOSTIC PRINTS
# ==============================================================================

    def _normalize_order_rows(self) -> None:
        """Ensure local tracking order descriptions, remarks, and states conform exactly to standard labels."""
        for order in self.paper_orders:
            if order.get("status") == "Closed" and order.get("entry_remarks") == "Paper entry active":
                order["entry_remarks"] = "Paper entry closed"
            if order.get("status") == "Closed" and not order.get("exit_remarks") and order.get("exit_reason"):
                order["exit_remarks"] = order.get("exit_reason")
            if order.get("status") == "QUOTE_ERROR" and order.get("quote_error") and not order.get("entry_remarks"):
                order["entry_remarks"] = order.get("quote_error")
            # Entry_Rejected: ensure exit_remarks shows "Rejected" so Orders block is unambiguous
            if order.get("status") == "Entry_Rejected":
                if not order.get("exit_remarks"):
                    order["exit_remarks"] = "Rejected"
                # Ensure entry_remarks carries the broker rejection reason (emsg / rejreason)
                if not order.get("entry_remarks") and order.get("broker_entry_status"):
                    order["entry_remarks"] = _broker_message(order["broker_entry_status"]) or "Entry Rejected by broker"

    def _log_terminal_status_locked(self, force: bool = False) -> None:
        """Write regular execution metrics update messages to the system terminal stdout."""
        now = datetime.now()
        if not force and self.last_terminal_status_at:
            elapsed = (now - self.last_terminal_status_at).total_seconds()
            if elapsed < TERMINAL_STATUS_SECONDS:
                return
        self.last_terminal_status_at = now
        clock = now.strftime("%H:%M:%S")
        mode = "REAL" if self.request and self.request.live_trade else "PAPER"
        active = "RUNNING" if self.active else "STOPPED"
        open_order = self._open_paper_order()
        
        open_text = "No open order"
        if open_order:
            status = open_order.get("status")
            symbol = open_order.get("tradingsymbol") or ""
            qty = open_order.get("quantity") or 0
            
            # Retrieve lot size from contract metadata to show current position in lots
            lot_size = None
            contract = open_order.get("trade_contract") or open_order.get("option_contract")
            if isinstance(contract, dict):
                lot_size = contract.get("lot_size")
                if not lot_size:
                    raw = contract.get("raw")
                    if isinstance(raw, dict):
                        lot_size = raw.get("LotSize") or raw.get("Lotsize") or raw.get("Lot Size")
            
            try:
                if lot_size:
                    lot_size = float(lot_size)
                    if lot_size > 0:
                        lots = qty / lot_size
                        lots_str = f"{int(lots)}" if lots.is_integer() else f"{lots:.2f}"
                        open_text = f"{status} {symbol} ({qty} qty / {lots_str} lots)"
                    else:
                        open_text = f"{status} {symbol} ({qty} qty)"
                else:
                    open_text = f"{status} {symbol} ({qty} qty)"
            except Exception:
                open_text = f"{status} {symbol} ({qty} qty)"
        
        # Get strategy OHLC metrics
        ohlc_text = ""
        if self.last_strategy and self.last_strategy.get("bars"):
            last_bar = self.last_strategy["bars"][-1]
            o = last_bar.get("open")
            h = last_bar.get("high")
            l = last_bar.get("low")
            c = last_bar.get("close")
            if all(v is not None for v in (o, h, l, c)):
                ohlc_text = f" | OHLC: {o:.2f}/{h:.2f}/{l:.2f}/{c:.2f}"

        # Get active broker name
        broker_name = "Unknown"
        if self.broker and hasattr(self.broker, "settings"):
            raw_broker = str(self.broker.settings.active_broker).lower()
            if raw_broker == "flattrade":
                broker_name = "FlatTrade"
            elif raw_broker == "zebu":
                broker_name = "Zebu"
            else:
                broker_name = raw_broker.capitalize()
        else:
            try:
                from config.settings import get_settings
                raw_broker = str(get_settings().active_broker).lower()
                if raw_broker == "flattrade":
                    broker_name = "FlatTrade"
                elif raw_broker == "zebu":
                    broker_name = "Zebu"
                else:
                    broker_name = raw_broker.capitalize()
            except Exception:
                pass

        if self.last_error or self.last_order_error:
            message = f"[{clock}] ERROR | {broker_name} | {mode.capitalize()} | {self.last_error or self.last_order_error}"
        else:
            message = (
                f"[{clock}] OK | {broker_name} | {mode.capitalize()} | {self.phase} | "
                f"Orders {len(self.paper_orders)} | {open_text} | MTM {self.day_pnl:.2f}{ohlc_text}"
            )
        self.last_terminal_message = message
        print(message, flush=True)

    def _find_order(self, key: str) -> dict[str, Any] | None:
        """Find order dictionary inside the day's local tracking list matching unique identifier."""
        # 1. Try exact match (checks UUID first if present, else fallback)
        for order in self.paper_orders:
            if self._order_row_key(order) == key:
                return order
        
        # 2. Try normalized fallback match (resilient to space vs 'T' mismatches and stale frontend caches)
        normalized_key = key.replace(" ", "T")
        for order in self.paper_orders:
            fallback = f"{order.get('entry_time') or order.get('time')}|{order.get('tradingsymbol') or ''}|{order.get('option_type') or ''}|{order.get('strike') or ''}"
            if fallback.replace(" ", "T") == normalized_key:
                return order
                
        # 3. Raise diagnostic error if both fail
        available_keys = [self._order_row_key(o) for o in self.paper_orders]
        raise RuntimeError(f"No active order matching key. Requested: {key} | Available: {available_keys}")

    @staticmethod
    def _order_row_key(order: dict[str, Any]) -> str:
        """Build the standard unique identifier string for indexing individual order rows."""
        return str(
            order.get("order_key")
            or order.get("entry_order_id")
            or f"{order.get('entry_time') or order.get('time')}|{order.get('tradingsymbol') or ''}|{order.get('option_type') or ''}|{order.get('strike') or ''}"
        )
