from __future__ import annotations
from utils.clock import market_now, market_time, candle_start

import csv
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import threading
from typing import Any
import uuid
import copy
import json
import os
from pathlib import Path

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
    
    # One shared reentrant lock owns mutable execution state. Candle computations
    # run outside it; legacy order polling may hold it during bounded HTTP calls.
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
    entries_enabled: bool = False
    recovery_required: bool = False
    _state_unreadable: bool = False
    state_path: Path | None = None
    broker_identity: str | None = None

    def __post_init__(self):
        # Every state mutation and snapshot uses the same reentrant lock.
        self.order_lock = self.lock
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                self.paper_orders = saved.get("orders", [])
                self.request = IntradayRunRequest.model_validate(saved["request"]) if saved.get("request") else None
                self.broker_identity = saved.get("broker_identity")
                self.trading_date = saved.get("trading_date")
                self.seen_signal_keys = set(saved.get("seen_signal_keys", []))
                self.day_pnl = sum(float(o.get("pnl") or 0) for o in self.paper_orders)
                self.recovery_required = bool(self._open_paper_order())
                for order in self.paper_orders:
                    order["chasing_active"] = False
                    order.pop("_management_active",None)
                    order.pop("_broker_sync_at", None)
                    if order.get("manual_submission") or order.get("status") in {"Entry_Submitting", "Exit_Submitting"}:
                        if self._unsettled(order) and not order.get("submission_attempt"):
                            order["manual_submission"]=False
                            self._refresh_execution_state(order)
                        else:
                            order.update(status="Recovery_Required",submission_unknown=True,manual_submission=False)
                if self.recovery_required:
                    self.last_error = "Recovered positions require broker reconnection; new entries paused"
            except Exception:
                self._state_unreadable = True
                self.recovery_required = True
                self.last_error = "Saved trading state is unreadable; reconcile account before trading"

    def _persist_state(self):
        if self._state_unreadable:
            raise RuntimeError("Refusing to overwrite unreadable execution state; reconcile account first")
        if not self.state_path:
            return
        with self.lock:
            data = dict(request=self.request.model_dump(mode="json") if self.request else None,
                        orders=self.paper_orders,broker_identity=self.broker_identity,
                        trading_date=self.trading_date,seen_signal_keys=sorted(self.seen_signal_keys))
            self.state_path.parent.mkdir(parents=True,exist_ok=True)
            import tempfile
            fd, temporary = tempfile.mkstemp(dir=self.state_path.parent, prefix="state-", suffix=".json")
            try:
                with os.fdopen(fd,"w",encoding="utf-8") as handle:
                    json.dump(data,handle,default=lambda v: v.isoformat() if hasattr(v,"isoformat") else str(v),allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary,self.state_path)
            except Exception:
                self.entries_enabled = False
                self.last_error = "Cannot persist trading state; new entries blocked"
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise

    @staticmethod
    def _identity(broker):
        import hashlib
        settings = broker.settings
        name = settings.active_broker
        user = getattr(broker,"verified_account_id",None) or getattr(settings, f"{name}_user_id", "")
        return hashlib.sha256(f"{name}:{user}".encode()).hexdigest()

    def attach_broker(self, broker):
        with self.lock:
            identity = self._identity(broker)
            if broker.connected and self._open_paper_order() and self.broker_identity and self.broker_identity != identity:
                raise RuntimeError("Cannot switch broker/account with unsettled positions")
            self.broker = broker
            if self.recovery_required and self.request and broker.connected:
                self.entries_enabled = False
                self.active = True
                self.recovery_required = False
                self.run_id = str(uuid.uuid4())
                self._start_workers()
                self.last_error = None

    def _start_workers(self):
        self.thread = threading.Thread(target=self._loop,args=(self.run_id,),daemon=True)
        self.order_thread = threading.Thread(target=self._order_watch_loop,args=(self.run_id,),daemon=True)
        self.thread.start()
        self.order_thread.start()

    def has_unsettled_orders(self):
        with self.lock:
            return bool(self._open_paper_order())


    # --------------------------------------------------------------------------
    # Life-Cycle Management (Start, Stop, Status Reports)
    # --------------------------------------------------------------------------

    def start(self, request: IntradayRunRequest) -> dict[str, Any]:
        with self.lock:
            if self.recovery_required:
                raise RuntimeError(self.last_error or "Reconnect the original broker to recover positions")
            if any(o.get("exit_intent",{}).get("active") for o in self.paper_orders):
                raise RuntimeError("Finish pending exit instructions before resuming entries")
            if any(o.get("submission_unknown") for o in self.paper_orders):
                raise RuntimeError("Unknown submission requires account reconciliation; new entries blocked")
            if request.live_trade and not request.continuous_execution:
                raise RuntimeError("Live trading requires bounded continuous limit execution")
            if self.active and self.entries_enabled:
                raise RuntimeError("Runner already active")
            if not self.broker or not self.broker.connected:
                raise RuntimeError("Connect the broker before starting the runner")
            if request.live_trade and not self.zebu.settings.live_trading_enabled:
                raise RuntimeError("Real Trade mode is blocked")
            identity = self._identity(self.broker)
            if self._open_paper_order():
                if self.broker_identity and self.broker_identity != identity:
                    raise RuntimeError("Open positions belong to a different broker/account")
                if self.request and self.request.model_dump() != request.model_dump():
                    raise RuntimeError("Open positions must retain their original configuration")
                if any(self._unsettled(o) for o in self.paper_orders):
                    raise RuntimeError("Settle pending orders before resuming entries")
            today = market_now().date().isoformat()
            if self.trading_date != today and not self._open_paper_order():
                self.paper_orders = []
                self.seen_signal_keys = set()
                self.day_pnl = 0
                self.trading_date = today
            if self.day_pnl >= request.max_profit or self.day_pnl <= request.max_loss:
                raise RuntimeError("Daily risk limit reached; new entries blocked")
            self.broker_identity = identity
            request.config.strike_interval = request.strike_interval
            request.paper_trade = not request.live_trade
            self.request = request
            cache_identity = (request.underlying,request.exchange,request.timeframe_minutes,today)
            if getattr(self,"_cache_identity",None) != cache_identity:
                self._cached_bars = None
                self._cache_identity = cache_identity
            self.started_at = market_now()
            self.entries_enabled = True
            self.last_error = None
            self._persist_state()
            if not self.active:
                self.active = True
                self.run_id = str(uuid.uuid4())
                self._start_workers()
            self.phase = "RUNNING"
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Pause entries; preserve monitoring until every position/order is settled."""
        with self.lock:
            self.entries_enabled = False
            self.phase = "ENTRIES_PAUSED_PROTECTION_RUNNING"
            for order in self.paper_orders:
                if order.get("status") == "Idle":
                    order.update(status="Entry_Rejected",entry_remarks="Unsubmitted entry cancelled on stop")
                for record in list(self._unsettled(order,"ENTRY")):
                    if not order.get("chasing_active") and not order.get("_management_active"):
                        self._cancel_confirmed(order,record)
            if not self._open_paper_order():
                self.active = False
                self.phase = "STOPPED"
            self._persist_state()
        return self.status()

    def status(self) -> dict[str, Any]:
        """Return a complete status snapshot of the runner."""
        with self.lock:
            self._normalize_order_rows()
            
            strategy_copy = None
            if self.last_strategy:
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
                "entries_enabled": self.entries_enabled,
                "recovery_required": self.recovery_required,
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
                "paper_orders": copy.deepcopy(self.paper_orders),
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
                "entries_enabled": self.entries_enabled,
                "recovery_required": self.recovery_required,
                "paper_orders": copy.deepcopy(self.paper_orders),
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
        while True:
            with self.lock:
                if not self.active or self.run_id != run_id or not self.request:
                    return
                request = self.request
                now = market_now()
                cutoff = _parse_clock(request.exit_time,time(15,15))
                if not request.positional_trade and now.time() >= cutoff:
                    self.entries_enabled = False
                    if request.order_product_type == "MIS":
                        self._close_open_paper_orders("SESSION_EXIT",now)
                if not self.entries_enabled and not self._open_paper_order():
                    self.active = False
                    self.phase = "STOPPED"
                    self._persist_state()
                    return
            self._tick(request,run_id)
            threading.Event().wait(max(2.,float(request.poll_seconds)))

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
                snapshots=self._prepare_watch_quotes(request)
                with self.order_lock:
                    self._update_open_order_prices(request, strategy, allow_exit=True, snapshots=snapshots)
                    self._log_orders_to_csv()
                with self.lock:
                    self.last_order_update = market_now().isoformat(timespec="seconds")
            except Exception as exc:
                with self.lock:
                    self.last_order_error = str(exc)
                    self.last_order_update = market_now().isoformat(timespec="seconds")
            event=getattr(self.broker,"order_event",None)
            if isinstance(event,threading.Event):
                event.wait(request.order_watch_seconds)
                event.clear()
            else:
                threading.Event().wait(request.order_watch_seconds)

    def _tick(self, request: IntradayRunRequest, run_id: str | None = None) -> None:
        """Fetch candles, compile technical strategy calculations, and flag signals."""
        try:
            now = market_now()
            start_clock = _parse_clock(request.start_time, NSE_OPEN)
            # Automatically calculate lookback calendar days to ensure we always fetch at least 250 candles
            tf_mins = request.timeframe_minutes
            bars_per_day = 375.0 / max(1, tf_mins)
            required_trading_days = int(500.0 / bars_per_day) + 1
            # Add weekend/non-trading days buffer (approx 1.5x + 2 days)
            required_calendar_days = max(request.lookback_days, int(required_trading_days * 1.5) + 2)
            start = _today_session_start(now, start_clock) - timedelta(days=required_calendar_days)
            with self.lock:
                self.phase = "FETCHING_BROKER_CANDLES"
                self.last_update = market_now().isoformat(timespec="seconds")
            
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

            # Brokers may include an unfinished candle. Trade only closed candles.
            current_start = candle_start(now,request.timeframe_minutes)
            fetched_bars = [dict(b,time=market_time(b["time"])) for b in fetched_bars if market_time(b["time"]) < current_start]
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
            with self.lock:
                self.phase = "RUNNING_STRATEGY"
                self.last_update = market_now().isoformat(timespec="seconds")
            strategy = run_strategy(bars, request.config)
            
            with self.lock:
                if run_id is not None and (self.run_id != run_id or not self.active):
                    return
            if self.entries_enabled and (request.paper_trade or request.live_trade):
                with self.lock:
                    self.phase = "UPDATING_PAPER_TRADES"
                    self.last_update = market_now().isoformat(timespec="seconds")
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
                    if not self.entries_enabled or (run_id is not None and self.run_id != run_id):
                        return
                    self._paper_trade_new_today_signals(strategy, request, now, prefetch_cache=prefetch)
                    self._log_orders_to_csv()
            with self.lock:
                self.last_strategy = strategy
                self.last_update = market_now().isoformat(timespec="seconds")
                self.phase = "WAITING_FOR_NEXT_POLL"
                self.last_error = None
                # Print terminal status on every tick so the console stays live
                # even when the browser UI tab is idle/sleeping.
                self._log_terminal_status_locked(force=True)
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
                self.phase = "ERROR"
                self.last_update = market_now().isoformat(timespec="seconds")
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
        if not self.entries_enabled:
            return
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
                self.seen_signal_keys.add(key)
                continue

            open_orders = [o for o in self.paper_orders if o.get("status") in OPEN_ORDER_STATUSES]
            valid_trades_count = sum(
                1 for o in self.paper_orders
                if o.get("status") not in {"Entry_Rejected", "QUOTE_ERROR"}
            )
            if valid_trades_count >= request.max_trades_per_day:
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
                    if o_ord.get("status") != "Closed":
                        self.last_skip_reason = "Waiting for opposite position exit fill"
                        skip_signal = True
                        break
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

            sl_price = None
            target_price = None

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
                "requested_quantity": request.qty,
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

            self._reset_risk_levels(order,request)
            self.paper_orders.append(order)
            self._persist_state()

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

    def _reset_risk_levels(self, order, request):
        if not request:
            return
        spot = request.sltp_instrument == "spot"
        entry = float(order.get("spot_entry") if spot else (order.get("option_entry") or 0))
        if entry <= 0:
            return
        direction = (1 if order.get("source_signal") == "BUY" else -1) if spot else (1 if order["side"] == "BUY" else -1)
        sl_move = entry*request.stoploss_value/100 if request.sl_type=="percentage" else request.stoploss_value
        target_move = entry*request.target_value/100 if request.target_type=="percentage" else request.target_value
        initial = entry-direction*sl_move
        if initial <= 0 or entry+direction*target_move <= 0:
            raise ValueError("SL/target levels must be positive")
        basis_changed = order.get("risk_instrument","option") != ("spot" if spot else "option")
        order.update(risk_entry=entry,risk_direction=direction,risk_instrument="spot" if spot else "option",initial_stoploss=initial)
        if not order.get("manual_stoploss"):
            old=None if basis_changed else order.get("stoploss")
            order["stoploss"] = initial if old is None else (max(old,initial) if direction==1 else min(old,initial))
        if not order.get("manual_target"):
            order["target"] = entry+direction*target_move

    def _risk_ltp(self, order, request, option_ltp):
        if request.sltp_instrument != "spot":
            return option_ltp
        name = _data_symbol(request.underlying)
        if name in INDEX_DATA_SYMBOLS:
            quote=self.zebu.get_single_index_quote(name)
            if not quote or quote.get("error") or quote.get("stale") or quote.get("ltp") is None:
                raise RuntimeError("Fresh underlying price unavailable for spot protection")
            return float(quote["ltp"])
        exchange=_data_exchange(request.exchange,request.underlying)
        token=self.zebu.search_token(exchange,name)
        return float(self.zebu.quote_ltp(self.zebu.get_quote(exchange,token)))

    def _prepare_watch_quotes(self, request):
        """Fetch risk quotes outside the state lock to keep control endpoints responsive."""
        import time as clock
        with self.order_lock:
            orders=[o for o in self.paper_orders if o.get("status") in OPEN_ORDER_STATUSES]
        snapshots={}
        for order in orders:
            quote,error=self._quote_snapshot(order)
            captured_at=clock.monotonic()
            risk,error_risk=None,None
            if quote and not error and quote.get("ltp") is not None:
                try:
                    risk=self._risk_ltp(order,request,float(quote["ltp"]))
                except Exception as exc:
                    error_risk=type(exc).__name__
            snapshots[self._order_row_key(order)]=(quote,error,risk,error_risk,captured_at)
        return snapshots

    def _start_position_sync(self, request):
        if getattr(self,"_positions_sync_active",False):
            return
        self._positions_sync_active=True
        def worker():
            try:
                self._sync_broker_positions(request)
            finally:
                with self.order_lock:
                    self._positions_sync_active=False
        threading.Thread(target=worker,daemon=True).start()

    def _update_open_order_prices(self, request, strategy=None, allow_exit=True, snapshots=None):
        strategy=strategy or {}
        if not any(o.get("submission_unknown") for o in self.paper_orders):
            self.last_order_error=None
        if request.live_trade:
            now=market_now().timestamp()
            if now-getattr(self,"_last_positions_sync_at",0)>=3:
                self._last_positions_sync_at=now
                self._start_position_sync(request)
        for order in list(self.paper_orders):
            self._consume_order_updates(order)
            self._sync_broker_order_state(order)
            self._start_execution_manager(order)
            snapshot=snapshots.get(self._order_row_key(order)) if snapshots is not None else None
            if snapshot is not None:
                import time as clock
                if clock.monotonic()-snapshot[4]>3:
                    snapshot=(None,"Risk quote expired while waiting for another request",None,None,snapshot[4])
            if order.get("status")=="Idle":
                self._process_entry_order(order,request,quote_result=(snapshot[0],snapshot[1]) if snapshot is not None else None)
            if int(order.get("quantity",0))<=0 or order.get("status") not in OPEN_ORDER_STATUSES:
                continue
            ltp=(snapshot[0] or {}).get("ltp") if snapshot is not None else self._current_order_ltp(order,0)
            if ltp is None:
                self.entries_enabled=False
                self.last_order_error="Fresh quote unavailable; new entries paused, protection retrying"
                continue
            order["option_ltp"]=ltp
            if not order.get("live_trade"):
                remaining=[]
                for pending in order.get("pending_manual_orders",[]):
                    marketable=(pending["action"]=="BUY" and ltp<=pending["price"]) or (pending["action"]=="SELL" and ltp>=pending["price"])
                    if marketable:
                        self._apply_manual_trade_fill(order,pending["action"],pending["quantity"],ltp)
                    else:
                        remaining.append(pending)
                order["pending_manual_orders"]=remaining
            else:
                order["pending_manual_orders"]=[dict(action=r["side"],quantity=r["quantity"]-r["filled"],price=r["price"],order_id=r["order_id"]) for r in self._unsettled(order,"MANUAL")]
            quantity=int(order.get("quantity",0))
            direction=1 if order["side"]=="BUY" else -1
            order["pnl"]=(ltp-order["option_entry"])*direction*quantity+order.get("realized_pnl",0.)
            if order.get("submission_unknown"):
                self.entries_enabled=False
                self.last_order_error="Unknown submission requires reconciliation; risk decisions paused"
                continue
            if quantity<=0:
                continue
            self._reset_risk_levels(order,request)
            risk_ltp=snapshot[2] if snapshot is not None else self._risk_ltp(order,request,ltp)
            if risk_ltp is None:
                self.entries_enabled=False
                self.last_order_error="Fresh risk price unavailable; protection retrying"
                continue
            order["risk_ltp"]=risk_ltp
            risk_direction=order["risk_direction"]
            if request.sltp_instrument != "strategy":
                self._update_trailing_stop(order,risk_ltp,request,risk_direction)
            reason=None
            latest=(strategy.get("signals") or [None])[-1]
            if latest and latest.get("side")!=order.get("source_signal") and market_time(latest["time"])>market_time(order.get("source_signal_time") or order["entry_time"]):
                reason="OPPOSITE_SIGNAL"
            reason=reason or self._sync_strategy_sltp(order,request,strategy)
            if request.sltp_instrument != "strategy" or order.get("manual_stoploss"):
                hit=risk_ltp<=order["stoploss"] if risk_direction==1 else risk_ltp>=order["stoploss"]
                if hit:
                    reason="TSL" if order.get("trail_active") or order.get("sl_moved_to_cost") else "SL"
            if request.sltp_instrument != "strategy" or order.get("manual_target"):
                hit=risk_ltp>=order["target"] if risk_direction==1 else risk_ltp<=order["target"]
                if hit and not reason:
                    reason="TARGET"
            if order.get("exit_intent",{}).get("active"):
                reason=order["exit_intent"].get("reason") or "MANUAL_EXIT"
            if reason and allow_exit:
                self._close_order(order,reason,market_now(),ltp)
        self.day_pnl=sum(float(o.get("pnl") or 0) for o in self.paper_orders)
        if self.day_pnl>=request.max_profit or self.day_pnl<=request.max_loss:
            self.entries_enabled=False
            self.last_skip_reason="Daily risk limit reached; flattening and cancelling pending entries"
            self._close_open_paper_orders("DAILY_LIMIT",market_now())
        self._persist_state()

    def _sync_broker_positions(self, request):
        broker=self.broker
        if not broker or not broker.connected:
            return
        try:
            positions=broker.get_positions()
            if not isinstance(positions,list) or any(not isinstance(p,dict) or p.get("stat")=="Not_Ok" for p in positions):
                raise RuntimeError("Broker positions unavailable; exposure is unknown")
            with self.order_lock:
                if self.broker is not broker:
                    return
                totals={}
                self.broker_day_pnl=0.
                for pos in positions:
                    symbol=pos.get("tsym")
                    exchange=str(pos.get("exch") or pos.get("exchange") or "").upper()
                    product=pos.get("prd")
                    key=(exchange,symbol,product)
                    totals[key]=totals.get(key,0)+int(float(pos.get("netqty",0)))
                    if str(symbol).startswith(request.underlying):
                        self.broker_day_pnl+=float(pos.get("urmtom") or 0)+float(pos.get("rpnl") or 0)
                for order in self.paper_orders:
                    if order.get("live_trade") and order.get("status") in OPEN_ORDER_STATUSES:
                        key=((order.get("trade_contract") or {}).get("exchange"),order.get("tradingsymbol"),PRODUCT_TYPE_MAP[request.order_product_type])
                        # Unknown exchange/product never supplies an exit-quantity cap.
                        if not any(k[0] and k[2] for k in totals) and positions:
                            order.pop("broker_net_qty",None)
                            continue
                        net=totals.get(key,0)
                        direction=1 if order["side"]=="BUY" else -1
                        order["broker_net_qty"]=max(0,net*direction)
                        order["broker_position_checked_at"]=market_now().isoformat()
        except Exception as exc:
            with self.order_lock:
                for order in self.paper_orders:
                    order.pop("broker_net_qty",None)
                self.entries_enabled=False
                self.last_order_error=str(exc)



    # ==============================================================================
    # SECTION 10: USER-FACING CONTROL PANEL & ADJUSTMENT ACTIONS
    # ==============================================================================

    def adjust_order(self, request: OrderAdjustRequest) -> dict[str, Any]:
        """User action handler: manually adjust SL, Target, or Trailing parameters for an active order."""
        with self.lock:
            order = self._find_order(request.order_key)
            if not order:
                raise RuntimeError("Order row not found.")
            if order.get("status") != "Active" or self._unsettled(order) or order.get("submission_unknown") or order.get("_management_active") or order.get("exit_intent",{}).get("active"):
                raise RuntimeError("Adjustments require a settled active position")
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
                        direction = int(order.get("risk_direction", 1 if order["side"] == "BUY" else -1))
                        cost_sl = float(order.get("risk_entry", order["option_entry"]))
                        current_sl = float(order["stoploss"])
                        if direction == 1:
                            if current_sl < cost_sl:
                                order["stoploss"] = cost_sl
                        else:
                            if current_sl > cost_sl:
                                order["stoploss"] = cost_sl
                        order["sl_moved_to_cost"] = True
                else:
                    order["move_sl_to_cost"] = False
            if request.move_sl_to_cost_points is not None:
                order["move_sl_to_cost_points"] = float(request.move_sl_to_cost_points)
                
            order["entry_remarks"] = order.get("entry_remarks") or "Manual controls updated"
            order["adjusted_at"] = market_now().isoformat(timespec="seconds")
            self.last_order_update = order["adjusted_at"]
        self._persist_state()
        return self.orders_status()

    def exit_open_order(self) -> dict[str, Any]:
        """User action handler: manually trigger immediate exit closure for any active trade."""
        with self.lock:
            order = self._open_paper_order()
        if not order:
            with self.lock:
                self.last_error = None
            return self.status()
            
        closed = self._close_order(order, "MANUAL_EXIT", market_now())
        with self.lock:
            if not closed:
                self.last_error = "Manual exit failed. Check quote availability or live exit order response."
            self.last_update = market_now().isoformat(timespec="seconds")
        return self.status()

    def manual_trade_action(self, order_key, action, quantity, price_str):
        with self.lock:
            order=self._find_order(order_key)
            if not order:
                raise ValueError("Order row not found")
            if order.get("status")!="Active" or order.get("manual_submission") or order.get("_management_active") or order.get("exit_intent",{}).get("active") or self._unsettled(order):
                raise RuntimeError("Manual action requires a settled active position")
            self._validate_order_size(order,int(quantity))
            reducing=action!=order["side"]
            reserved=sum(p["quantity"] for p in order.get("pending_manual_orders",[]) if p["action"]!=order["side"])
            if reducing and quantity>int(order["quantity"])-reserved:
                raise ValueError("Reduction exceeds available position; reversal is not supported")
            if not reducing and not self.entries_enabled:
                raise RuntimeError("New exposure is paused")
            order["manual_submission"]=True
            self._persist_state()
        try:
            quote,error=self._quote_snapshot(order)
            if not quote:
                raise ValueError(error or "Fresh quote required")
            is_market=price_str.strip().lower() in {"at mkt","market","","mkt"}
            raw=(quote.get("best_sell" if action=="BUY" else "best_buy") or quote.get("ltp")) if is_market else float(price_str)
            price=self._round_to_tick(raw,order.get("tick_size") or .05,action)
        except Exception:
            with self.lock:
                order["manual_submission"]=False
                self._persist_state()
            raise
        with self.lock:
            if not order.get("live_trade"):
                crossing=quote.get("best_sell" if action=="BUY" else "best_buy") or quote.get("ltp")
                if (action=="BUY" and price<crossing) or (action=="SELL" and price>crossing):
                    order.setdefault("pending_manual_orders",[]).append(dict(action=action,quantity=quantity,price=price))
                else:
                    self._apply_manual_trade_fill(order,action,quantity,float(crossing))
                order["manual_submission"]=False
                self._persist_state()
                return self.orders_status()
        record=self._submit(order,action,price,"MANUAL",self.request,quantity)
        with self.lock:
            order["manual_submission"]=False
            if record:
                order.setdefault("pending_manual_orders",[]).append(dict(action=action,quantity=quantity,price=price,order_id=record["order_id"]))
            self._persist_state()
        return self.orders_status()

    def cancel_manual_trade_action(self, order_key):
        with self.lock:
            order=self._find_order(order_key)
            if not order:
                raise ValueError("Order row not found")
            if order.get("manual_submission"):
                raise RuntimeError("Manual submission is still in progress")
            for record in list(self._unsettled(order,"MANUAL")):
                if not self._cancel_confirmed(order,record):
                    raise RuntimeError("Manual order cancellation not confirmed; still tracked")
            order["pending_manual_orders"]=[]
            self._persist_state()
        return self.orders_status()

    def _apply_manual_trade_fill(self, order, action, quantity, price):
        old_qty=int(order.get("quantity",0))
        old_entry=float(order.get("option_entry") or 0)
        direction=1 if order["side"]=="BUY" else -1
        if action==order["side"]:
            order["quantity"]=old_qty+quantity
            order["option_entry"]=(old_entry*old_qty+price*quantity)/(old_qty+quantity)
            self._reset_risk_levels(order,self.request)
        else:
            if quantity>old_qty:
                self._unknown(order,"Broker reduction exceeded tracked quantity; reconcile actual position")
                raise RuntimeError("Reduction exceeds tracked quantity")
            order["quantity"]=old_qty-quantity
            order["realized_pnl"]=order.get("realized_pnl",0.)+(price-old_entry)*direction*quantity
            order["option_ltp"]=price
            order["pnl"]=order["realized_pnl"]+(price-old_entry)*direction*order["quantity"]
            if order["quantity"]==0 and not order.get("live_trade"):
                self._mark_order_closed(order,"MANUAL_EXIT",market_now(),price,remaining_qty=0)
        self._persist_state()



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
        now = market_now()
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
