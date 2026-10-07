import csv
from datetime import datetime, time, timedelta
import threading
from typing import Any

from config.settings import BASE_DIR
from models.schemas import IntradayRunRequest
from utils.helpers import (
    _broker_avg_price,
    _broker_filled_qty,
    _broker_message,
    _broker_order_id,
    _broker_value,
    _clean_order_id,
    _contract_name,
    _flatten_broker_orders,
    PRODUCT_TYPE_MAP,
    ACTIVE_ORDER_STATUSES,
    OPEN_ORDER_STATUSES,
    FINAL_ORDER_STATUSES,
)


class OrderManagerMixin:
    """Mixin class for IntradayRunner handling order entry/exit formatting, broker API interactions, and CSV order logging."""

    _BROKER_SYNC_INTERVAL: float = 2.0

    def _entry_waits_for_blank_limit(self, request: IntradayRunRequest) -> bool:
        """Check if limit trigger values are blank for conditional orders."""
        return request.entry_order_mode in {"Limit_Below", "Limit_Above"} and request.entry_limit_price is None

    @staticmethod
    def _round_to_tick(price: float, tick: float, side: str) -> float:
        """Round decimal price values strictly according to exchange contract tick increments."""
        tick = tick or 0.05
        steps = price / tick
        rounded_steps = int(steps) if side == "SELL" else int(steps + 0.999999)
        return round(max(tick, rounded_steps * tick), 2)

    def _entry_limit_price(self, order: dict[str, Any], request: IntradayRunRequest) -> tuple[float | None, str | None]:
        """Formulate the exact limit price for entry placement under different modes."""
        quote, error = self._quote_snapshot(order)
        if not quote:
            return None, error
        ltp = quote.get("ltp")
        tick = float(quote.get("tick_size") or order.get("tick_size") or 0.05)
        order["option_ltp"] = ltp if ltp is not None else order.get("option_ltp")
        mode = request.entry_order_mode
    
        if mode == "Aggressive_Entry":
            raw = (quote.get("best_buy") if order["side"] == "BUY" else quote.get("best_sell")) or ltp
            if raw is None:
                return None, "No quote price available for aggressive entry."
            raw = raw + tick if order["side"] == "BUY" else raw - tick
            return self._round_to_tick(float(raw), tick, order["side"]), None
        
        if mode == "True_Limit_LTP":
            return (self._round_to_tick(float(ltp), tick, order["side"]), None) if ltp is not None else (None, "No LTP for entry.")
        
        if request.entry_limit_price is None:
            return None, None
        
        trigger = float(request.entry_limit_price)
        if mode == "Limit_Below" and (ltp is None or ltp > trigger):
            return None, None
        if mode == "Limit_Above" and (ltp is None or ltp < trigger):
            return None, None
        return self._round_to_tick(trigger, tick, order["side"]), None

    def _exit_limit_price(self, order: dict[str, Any], request: IntradayRunRequest, exit_side: str) -> tuple[float | None, str | None]:
        """Formulate the exact limit price for exit placement under different modes."""
        if request.exit_order_mode == "False":
            return None, "Exit order mode is False."
        quote, error = self._quote_snapshot(order)
        if not quote:
            return None, error
        ltp = quote.get("ltp")
        tick = float(quote.get("tick_size") or order.get("tick_size") or 0.05)
        order["option_ltp"] = ltp if ltp is not None else order.get("option_ltp")
        mode = request.exit_order_mode
    
        if mode == "Aggressive_Exit":
            raw = quote.get("best_buy") if exit_side == "SELL" else quote.get("best_sell")
            raw = raw if raw is not None else ltp
            if raw is None:
                return None, "No quote price available for aggressive exit."
            raw = raw + tick if exit_side == "SELL" else raw - tick
            return self._round_to_tick(float(raw), tick, exit_side), None
        
        if mode == "True_Limit_LTP":
            return (self._round_to_tick(float(ltp), tick, exit_side), None) if ltp is not None else (None, "No LTP for exit.")
        
        trigger = request.exit_limit_price if request.exit_limit_price is not None else order.get("target")
        if trigger is None:
            return None, None
        
        trigger = float(trigger)
        if mode == "Limit_Below" and (ltp is None or ltp > trigger):
            return None, None
        if mode == "Limit_Above" and (ltp is None or ltp < trigger):
            return None, None
        return self._round_to_tick(trigger, tick, exit_side), None

    def _process_entry_order(self, order: dict[str, Any], request: IntradayRunRequest) -> None:
        """Trigger broker entry order placement once trigger parameters match."""
        if order.get("entry_order_id") or order.get("status") not in {"Idle"}:
            return

        # 1. If trade_contract is missing or contains an "error", retry contract resolution
        contract = order.get("trade_contract")
        if not contract or contract.get("error"):
            strike = order.get("strike")
            option_type = order.get("option_type")
            if strike is not None and option_type is not None:
                new_contract = self._resolve_contract(request, int(strike), option_type)
                if new_contract and not new_contract.get("error"):
                    order["trade_contract"] = new_contract
                    order["option_contract"] = new_contract
                    order["tradingsymbol"] = _contract_name(new_contract)
                    order["tick_size"] = request.tick_size or new_contract.get("tick_size") or new_contract.get("raw", {}).get("TickSize") or 0.05
                    order["quote_error"] = None
                    contract = new_contract
                else:
                    err_msg = (new_contract.get("error") if new_contract else None) or "Failed to resolve contract"
                    order["quote_error"] = err_msg
                    order["entry_remarks"] = f"Contract resolution retry failed: {err_msg}"
                    order["quote_retry_count"] = order.get("quote_retry_count", 0) + 1
                    if order["quote_retry_count"] >= 10:
                        order["status"] = "QUOTE_ERROR"
                    return

        # 2. Get entry limit price
        price, error = self._entry_limit_price(order, request)
        if price is None or error:
            err_msg = error or "Entry limit price not available yet"
            order["quote_error"] = err_msg
            order["entry_remarks"] = f"Quote retry: {err_msg}"
            order["quote_retry_count"] = order.get("quote_retry_count", 0) + 1
            if order["quote_retry_count"] >= 10:
                order["status"] = "QUOTE_ERROR"
                order["entry_remarks"] = f"Permanent quote error: {err_msg}"
            return

        # 3. If a valid price is found, proceed with entry
        order["quote_retry_count"] = 0
        order["quote_error"] = None
        order["option_entry"] = price
        order["option_ltp"] = price
        order["entry_order_price"] = price

        if order.get("stoploss") is None:
            sl_val = self._sl_price(price, request)
            order["initial_stoploss"] = sl_val
            order["stoploss"] = sl_val
        if order.get("target") is None:
            tgt_val = self._target_price(price, request)
            order["target"] = tgt_val

        if not request.live_trade:
            order["status"] = "Active"
            order["entry_remarks"] = "Paper entry active"
            return
        
        order["paper"] = False
        if request.enable_price_chasing:
            order["chasing_active"] = True
            order["status"] = "Entry_Pending"
            order["entry_remarks"] = "Starting entry price chasing..."
            threading.Thread(
                target=self._run_price_chasing_loop,
                args=(order, order["side"], price, "ENTRY", request, int(order["quantity"]), True),
                daemon=True
            ).start()
        else:
            response, live_error = self._place_limit_order(order, order["side"], price, "ENTRY", request)
            order["entry_order_response"] = response
            order["entry_order_id"] = self._order_id(response)
            order["entry_remarks"] = live_error or _broker_message(response)
        
            if live_error and not order["entry_order_id"]:
                order["status"] = "Entry_Rejected"
                order["quote_error"] = live_error
            elif order["entry_order_id"]:
                order["status"] = "Entry_Pending"
            else:
                order["status"] = "Entry_Rejected"

    def _place_limit_order(
        self,
        order: dict[str, Any],
        side: str,
        price: float,
        reason: str,
        request: IntradayRunRequest,
        quantity: int | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Perform raw limit order placement calling Zebu Client API."""
        try:
            qty = quantity if quantity is not None else int(order["quantity"])
            response = self.zebu.place_order(
                exchange=order["trade_contract"]["exchange"],
                tradingsymbol=order["tradingsymbol"],
                side=side,
                quantity=qty,
                product_type=PRODUCT_TYPE_MAP.get(request.order_product_type, "I"),
                price_type="LMT",
                price=price,
                trigger_price=0,
                confirm_live=True,
            )
            if response.get("paper"):
                return response, response.get("message", "Live order blocked.")
            if response.get("stat") == "Not_Ok":
                return response, response.get("emsg", f"{reason} order rejected.")
            return response, None
        except Exception as exc:
            return None, f"{reason} order failed: {exc}"

    def _place_market_order(
        self,
        order: dict[str, Any],
        side: str,
        reason: str,
        request: IntradayRunRequest,
        quantity: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Perform aggressive limit order placement at any cost to emulate market order execution."""
        try:
            quote_val = 0.0
            contract = order.get("option_contract")
            if contract:
                quote_val, _ = self._quote_ltp(contract)
            if not quote_val:
                quote_val = float(order.get("option_ltp") or order.get("option_entry") or 100.0)
        
            tick = float(order.get("tick_size") or 0.05)
            if side == "BUY":
                aggressive_price = quote_val * 1.10
            else:
                aggressive_price = quote_val * 0.90
            
            aggressive_price = self._round_to_tick(aggressive_price, tick, side)
            print(f"[SWEEP] Placing aggressive limit {side} order at any cost: price={aggressive_price:.2f} qty={quantity} (LTP={quote_val:.2f})", flush=True)

            response = self.zebu.place_order(
                exchange=order["trade_contract"]["exchange"],
                tradingsymbol=order["tradingsymbol"],
                side=side,
                quantity=quantity,
                product_type=PRODUCT_TYPE_MAP.get(request.order_product_type, "I"),
                price_type="LMT",
                price=aggressive_price,
                trigger_price=0,
                confirm_live=True,
            )
            if response.get("paper"):
                return response, response.get("message", "Live order blocked.")
            if response.get("stat") == "Not_Ok":
                return response, response.get("emsg", f"{reason} aggressive limit sweep rejected.")
            return response, None
        except Exception as exc:
            return None, f"{reason} aggressive limit sweep failed: {exc}"

    def _run_price_chasing_loop(
        self,
        order: dict[str, Any],
        side: str,
        initial_price: float,
        reason: str,
        request: IntradayRunRequest,
        total_quantity: int,
        is_entry: bool,
    ) -> None:
        """Asynchronous execution loop to guarantee 100% order execution via price chasing and market sweep fallback."""
        import time
        from datetime import datetime

        print(f"[CHASE] Starting chasing thread for order={order.get('id')} side={side} qty={total_quantity} price={initial_price}", flush=True)

        current_order_id = order.get("entry_order_id" if is_entry else "exit_order_id")
        current_price = initial_price
    
        filled_qty_so_far = 0
        order_fills = {}  # mapping order_id -> (filled_qty, avg_price)
    
        if current_order_id:
            order_fills[current_order_id] = (0, current_price)
        
        retry_count = 0
        start_time = datetime.now()
    
        timeout_seconds = request.chase_timeout_seconds
        max_retries = request.chase_max_retries
        slippage_pct = request.chase_slippage_pct
        sweep_market = request.chase_sweep_market
        tick = order.get("tick_size") or 0.05

        def get_total_filled():
            return sum(qty for qty, _ in order_fills.values())

        # Main polling loop
        while get_total_filled() < total_quantity:
            now_time = datetime.now()
            elapsed = (now_time - start_time).total_seconds()
        
            if elapsed >= timeout_seconds or retry_count >= max_retries:
                print(f"[CHASE] Timeout/retries exceeded (elapsed={elapsed:.1f}s, retries={retry_count}). Triggering safeguard...", flush=True)
                break
            
            if not current_order_id:
                remaining = total_quantity - get_total_filled()
                if remaining <= 0:
                    break
                print(f"[CHASE] Placing new limit order for remaining={remaining} @ price={current_price:.2f}", flush=True)
                live_response, live_error = self._place_limit_order(order, side, current_price, reason, request, quantity=remaining)
                current_order_id = self._order_id(live_response)
            
                with self.order_lock:
                    if is_entry:
                        order["entry_order_id"] = current_order_id
                        order["entry_order_price"] = current_price
                        order["entry_remarks"] = live_error or _broker_message(live_response)
                    else:
                        order["exit_order_id"] = current_order_id
                        order["exit_order_price"] = current_price
                        order["exit_remarks"] = live_error or _broker_message(live_response)
                    
                if not current_order_id:
                    print(f"[CHASE] Failed to place limit order: {live_error}. Sleeping before retry...", flush=True)
                    time.sleep(1.0)
                    retry_count += 1
                    continue
                
                order_fills[current_order_id] = (0, current_price)
                retry_count += 1
            
            time.sleep(0.5)
            try:
                broker_status = self._broker_order_status(str(current_order_id))
                state = self._mapped_broker_state(broker_status)
                filled_qty = _broker_filled_qty(broker_status)
                avg_price = _broker_avg_price(broker_status) or current_price
            
                order_fills[current_order_id] = (filled_qty, avg_price)
            
                if state == "filled" or get_total_filled() >= total_quantity:
                    break
                
                if state == "rejected":
                    print(f"[CHASE] Order {current_order_id} rejected. Resetting order ID to re-fire.", flush=True)
                    current_order_id = None
                    continue
                
                if is_entry:
                    new_price, price_error = self._entry_limit_price(order, request)
                else:
                    new_price, price_error = self._exit_limit_price(order, request, side)
            
                if new_price is None or price_error:
                    ltp, err = self._quote_ltp(order.get("option_contract"))
                    new_price = ltp
                
                if new_price is not None:
                    should_modify = False
                    if side == "BUY" and new_price > current_price:
                        should_modify = True
                    elif side == "SELL" and new_price < current_price:
                        should_modify = True
                    
                    if should_modify:
                        print(f"[CHASE] Price moved from {current_price:.2f} to calculated {new_price:.2f}. Cancelling order {current_order_id}...", flush=True)
                    
                        try:
                            self.zebu.cancel_order(str(current_order_id))
                        except Exception as e:
                            print(f"[CHASE] Error cancelling order: {e}", flush=True)
                        
                        cancel_confirmed = False
                        for _ in range(25):  # up to 5 seconds
                            time.sleep(0.2)
                            b_stat = self._broker_order_status(str(current_order_id))
                            b_state = self._mapped_broker_state(b_stat)
                            filled_qty = _broker_filled_qty(b_stat)
                            avg_price = _broker_avg_price(b_stat) or current_price
                            order_fills[current_order_id] = (filled_qty, avg_price)
                        
                            if b_state in {"cancelled", "rejected", "filled"} or get_total_filled() >= total_quantity:
                                cancel_confirmed = True
                                break
                            
                        print(f"[CHASE] Cancel confirmation check done. Filled qty: {get_total_filled()}/{total_quantity}", flush=True)
                        if get_total_filled() >= total_quantity:
                            break
                        
                        if side == "BUY":
                            max_allowed = initial_price * (1 + slippage_pct / 100.0)
                            current_price = min(new_price, max_allowed)
                        else:
                            min_allowed = initial_price * (1 - slippage_pct / 100.0)
                            current_price = max(new_price, min_allowed)
                        
                        current_price = self._round_to_tick(current_price, tick, side)
                        current_order_id = None
            except Exception as e:
                print(f"[CHASE] Error in chase loop: {e}", flush=True)
                time.sleep(1.0)
            
        # --- Safeguard / Market Sweep Fallback ---
        final_filled = get_total_filled()
        if final_filled < total_quantity:
            remaining = total_quantity - final_filled
            print(f"[CHASE] Safeguard triggered. Final filled={final_filled}/{total_quantity}. Remaining={remaining}", flush=True)
        
            if current_order_id:
                try:
                    print(f"[CHASE] Cancelling limit order {current_order_id} before sweep...", flush=True)
                    self.zebu.cancel_order(str(current_order_id))
                except Exception as e:
                    print(f"[CHASE] Error cancelling order during safeguard: {e}", flush=True)
                
                for _ in range(15):  # up to 3 seconds
                    time.sleep(0.2)
                    b_stat = self._broker_order_status(str(current_order_id))
                    b_state = self._mapped_broker_state(b_stat)
                    filled_qty = _broker_filled_qty(b_stat)
                    avg_price = _broker_avg_price(b_stat) or current_price
                    order_fills[current_order_id] = (filled_qty, avg_price)
                    if b_state in {"cancelled", "rejected", "filled"}:
                        break
                    
            final_filled = get_total_filled()
            remaining = total_quantity - final_filled
        
            if remaining > 0:
                if sweep_market:
                    print(f"[CHASE] Sweeping remaining {remaining} quantity with Market order...", flush=True)
                    sweep_response, sweep_error = self._place_market_order(order, side, reason, request, quantity=remaining)
                    sweep_id = self._order_id(sweep_response)
                
                    if sweep_id:
                        sweep_filled = 0
                        sweep_price = current_price
                        for _ in range(15):  # up to 3 seconds
                            time.sleep(0.2)
                            b_stat = self._broker_order_status(str(sweep_id))
                            b_state = self._mapped_broker_state(b_stat)
                            if b_state == "filled":
                                sweep_filled = _broker_filled_qty(b_stat)
                                sweep_price = _broker_avg_price(b_stat) or current_price
                                break
                        if sweep_filled > 0:
                            order_fills[sweep_id] = (sweep_filled, sweep_price)
                    else:
                        print(f"[CHASE] Market sweep order failed to place: {sweep_error}", flush=True)
                else:
                    print(f"[CHASE] Market sweep disabled. Remaining quantity left unfilled.", flush=True)

        final_filled = get_total_filled()
        total_cost = sum(qty * prc for qty, prc in order_fills.values())
        avg_fill_price = total_cost / final_filled if final_filled > 0 else initial_price

        print(f"[CHASE] Finished. Total filled={final_filled}/{total_quantity} @ avg_price={avg_fill_price:.2f}", flush=True)

        with self.lock:
            with self.order_lock:
                if is_entry:
                    if final_filled > 0:
                        order["status"] = "Active"
                        order["option_entry"] = avg_fill_price
                        order["entry_order_price"] = avg_fill_price
                    
                        sl_val = self._sl_price(avg_fill_price, request)
                        order["initial_stoploss"] = sl_val
                        order["stoploss"] = sl_val
                        tgt_val = self._target_price(avg_fill_price, request)
                        order["target"] = tgt_val
                    
                        order["entry_remarks"] = f"Filled via Chasing @ {avg_fill_price:.2f}"
                    else:
                        order["status"] = "Entry_Rejected"
                        order["entry_remarks"] = "Chasing completed with 0 filled quantity."
                else:
                    if final_filled > 0:
                        self._mark_order_closed(order, reason, datetime.now(), avg_fill_price, remaining_qty=final_filled)
                        order["exit_remarks"] = f"Exited via Chasing @ {avg_fill_price:.2f}"
                    else:
                        order["status"] = "Active"
                        order["exit_order_id"] = None
                        order["exit_order_price"] = None
                        order["exit_remarks"] = "Chasing failed to execute any exit quantity."
                    
                order["chasing_active"] = False

        self._normalize_order_rows()

    def _sync_broker_order_state(self, order: dict[str, Any]) -> None:
        """Poll the broker for order status updates, throttled to every 2 s per order.

        Only Entry_Pending and Exit_Pending orders trigger REST API calls.
        Active orders only need LTP quotes (handled separately in _current_order_ltp),
        so they are skipped here to keep the order-watch loop fast.
        """
        if not order.get("live_trade"):
            return

        if order.get("chasing_active"):
            return

        needs_sync = (
            (order.get("entry_order_id") and order.get("status") == "Entry_Pending")
            or (order.get("exit_order_id") and order.get("status") == "Exit_Pending")
        )
        if not needs_sync:
            return

        # Throttle: skip if we polled this order less than 2 s ago
        last_sync = order.get("_broker_sync_at")
        now_ts = datetime.now().timestamp()
        if last_sync is not None and (now_ts - last_sync) < self._BROKER_SYNC_INTERVAL:
            return
        order["_broker_sync_at"] = now_ts

        if order.get("entry_order_id") and order.get("status") == "Entry_Pending":
            broker = self._broker_order_status(str(order["entry_order_id"]))
            order["broker_entry_status"] = broker
            state = self._mapped_broker_state(broker)
            if state == "filled":
                avg_price = _broker_avg_price(broker)
                if avg_price is not None:
                    order["option_entry"] = avg_price
                    order["entry_order_price"] = avg_price
                order["status"] = "Active"
                order["entry_remarks"] = _broker_message(broker) or order.get("entry_remarks")
            elif state == "rejected":
                reason = _broker_message(broker) or "Entry order rejected by broker"
                order["status"] = "Entry_Rejected"
                order["entry_remarks"] = reason
                order["quote_error"] = reason
                self.last_skip_reason = (
                    f"Entry rejected for {order.get('tradingsymbol')}: {reason}. "
                    "Waiting for the next candle signal."
                )
                print(
                    f"[ORDER] Entry REJECTED for {order.get('tradingsymbol')} "
                    f"id={order.get('entry_order_id')} reason={order.get('entry_remarks')}",
                    flush=True,
                )

        if order.get("exit_order_id") and order.get("status") == "Exit_Pending":
            broker = self._broker_order_status(str(order["exit_order_id"]))
            order["broker_exit_status"] = broker
            state = self._mapped_broker_state(broker)
            if state == "filled":
                exit_price = _broker_avg_price(broker) or order.get("option_ltp")
                self._mark_order_closed(order, order.get("pending_exit_reason") or "Closed", datetime.now(), exit_price)
                order["exit_remarks"] = _broker_message(broker) or order.get("exit_remarks")
            elif state == "rejected":
                exit_id = order.get("exit_order_id")
                reason = _broker_message(broker) or "Exit order rejected by broker"
                order["status"] = "Active"
                order["exit_remarks"] = f"Exit rejected: {reason}"
                order["last_exit_error"] = order["exit_remarks"]
                order["exit_order_id"] = None
                order["exit_order_price"] = None
                self.last_skip_reason = (
                    f"Exit rejected for {order.get('tradingsymbol')}: {reason}. "
                    "Order row is Active again for the next exit check."
                )
                print(
                    f"[ORDER] Exit REJECTED for {order.get('tradingsymbol')} "
                    f"id={order.get('exit_order_id')} reason={order.get('exit_remarks')} — resetting to Active",
                    flush=True,
                )

    def _broker_order_status(self, order_id: str) -> Any:
        """Query individual order history from Zebu; falls back to order-book if history returns nothing."""
        normalized_order_id = _clean_order_id(order_id)
        try:
            history = self.zebu.single_order_history(order_id)
            best_item = self._best_broker_status(_flatten_broker_orders(history), "")
            if best_item is not None:
                return best_item
            if isinstance(history, dict):
                if history.get("stat") != "Not_Ok":
                    if history.get("status") or history.get("Status") or history.get("stat"):
                        return history
        except Exception as exc:
            print(f"[ORDER] single_order_history error for {order_id}: {exc}", flush=True)

        # Fallback: scan order book for this order id (covers REJECTED orders that
        # may not appear in single_order_history on Zebu/Shoonya API)
        order_book_fetched = False
        found_item = None
        try:
            order_book = self.zebu.get_order_book()
            if isinstance(order_book, (list, dict)):
                if isinstance(order_book, dict) and order_book.get("stat") == "Not_Ok":
                    pass
                else:
                    order_book_fetched = True
                    found_item = self._best_broker_status(_flatten_broker_orders(order_book), normalized_order_id)
        except Exception as exc:
            print(f"[ORDER] get_order_book fallback error for {order_id}: {exc}", flush=True)

        if found_item is not None:
            return found_item

        # If history query failed/returned nothing and order book fetch succeeded but order ID is not found,
        # it is a broker rejection (e.g. order rejected instantly before registering or invalid).
        if order_book_fetched:
            return {
                "status": "REJECTED",
                "stat": "Not_Ok",
                "rejreason": "Order not found in broker history or order book"
            }

        return None

    @staticmethod
    def _best_broker_status(rows: list[dict[str, Any]], order_id: str) -> dict[str, Any] | None:
        """Pick the most meaningful broker row for one order id."""
        best_item = None
        best_rank = 0
        for item in rows:
            item_order_id = _broker_order_id(item)
            if order_id and item_order_id and item_order_id != order_id:
                continue
            if order_id and not item_order_id:
                continue
            state = OrderManagerMixin._mapped_broker_state(item)
            rank = 1
            if state == "rejected":
                rank = 2
            elif state == "filled":
                rank = 3
            if rank > best_rank or best_item is None:
                best_rank = rank
                best_item = item
        return best_item

    @staticmethod
    def _mapped_broker_state(broker: Any) -> str:
        """Decode multi-faceted broker response statuses into standard states: pending, filled, rejected."""
        if not isinstance(broker, dict):
            return "pending"
        # 1. Check the explicit 'status' field first — most reliable for Zebu/Shoonya
        explicit_status = str(
            _broker_value(
                broker,
                ("status", "Status", "order_status", "ordstatus", "ord_status", "stat"),
            )
            or ""
        ).upper().strip()
        if explicit_status in ("COMPLETE", "FILLED", "TRADED", "EXECUTED"):
            return "filled"
        if explicit_status in ("REJECTED", "CANCELLED", "CANCELED", "FAILED", "REJECT", "NOT_OK"):
            return "rejected"
        # 2. Fallback: substring scan across all relevant fields
        raw = " ".join(
            str(value)
            for key, value in broker.items()
            if any(word in str(key).lower() for word in ("status", "stat", "rej", "remark", "msg", "reason", "text"))
        ).lower()
        if any(word in raw for word in ("complete", "filled", "traded", "executed")):
            return "filled"
        # Comprehensive rejection detection: handles Zebu/Shoonya broker rejection states
        # including 'REJECTED', 'CANCELLED', 'CANCELED', 'not_ok', insufficient funds msgs
        if any(word in raw for word in ("reject", "cancel", "fail", "insufficient", "no fund", "margin", "block", "denied", "not found", "not_ok", "not ok", "nonsqroff", "negativecash", "not a multiple")):
            return "rejected"
        # If stat is explicitly Not_Ok with no order id context → rejection
        if str(broker.get("stat", "")).lower() == "not_ok" and not _broker_order_id(broker):
            return "rejected"
        return "pending"

    @staticmethod
    def _order_id(response: dict[str, Any] | None) -> str | None:
        """Safely extract order ID string from a placing response dict."""
        return _broker_order_id(response)

    def _close_open_paper_orders(self, reason: str, now: datetime) -> None:
        """Close out all active tracking order rows at session exit."""
        total = 0.0
        for order in self.paper_orders:
            if order["status"] not in ACTIVE_ORDER_STATUSES:
                total += float(order.get("pnl") or 0)
                continue
            self._close_order(order, reason, now)
            total += float(order["pnl"])
        self.day_pnl = total

    def _close_order(self, order: dict[str, Any], reason: str, now: datetime, ltp: float | None = None) -> bool:
        """Trigger an order close request by submitting opposite order parameters."""
        if ltp is None:
            ltp = self._current_order_ltp(order, float(order.get("option_ltp") or 0))
        if ltp is None:
            ltp = order.get("option_ltp")
        if ltp is None:
            ltp = order.get("option_entry")
        if ltp is None:
            return False
        if order.get("status") not in ACTIVE_ORDER_STATUSES:
            return False
        if order.get("exit_order_id"):
            return True
        
        if order.get("live_trade") and order.get("status") == "Active":
            if not self.request:
                return False
        
            # Cancel any pending manual limit orders first to free up margin and prevent order conflicts
            pending_orders = order.get("pending_manual_orders", [])
            if pending_orders:
                import time
                for p_order in pending_orders:
                    p_id = p_order.get("order_id")
                    if p_id:
                        try:
                            print(f"[CloseOrder] Cancelling pending manual order {p_id} before exit...", flush=True)
                            self.zebu.cancel_order(str(p_id))
                        except Exception as e:
                            print(f"[CloseOrder] Error cancelling pending manual order {p_id}: {e}", flush=True)
                order["pending_manual_orders"] = []
                time.sleep(0.15)  # Brief sleep to allow broker to release blocked margin
            
            algo_qty = int(order["quantity"])
            broker_qty = order.get("broker_net_qty", algo_qty)
            exit_qty = min(algo_qty, broker_qty)
        
            if exit_qty <= 0:
                print(f"[CloseOrder] Exit quantity capped to {exit_qty} <= 0. Skipping broker order and marking local order closed.", flush=True)
                self._mark_order_closed(order, reason, now, ltp)
                return True

            exit_side = "SELL" if order["side"] == "BUY" else "BUY"
            price, price_error = self._exit_limit_price(order, self.request, exit_side)
            if price_error:
                order["exit_remarks"] = price_error
                order["last_exit_error"] = price_error
                return False
            if price is None:
                return False
            
            if self.request.enable_price_chasing:
                order["chasing_active"] = True
                order["status"] = "Exit_Pending"
                order["pending_exit_reason"] = reason
                order["exit_remarks"] = "Starting exit price chasing..."
                threading.Thread(
                    target=self._run_price_chasing_loop,
                    args=(order, exit_side, price, reason, self.request, exit_qty, False),
                    daemon=True
                ).start()
                return True
            else:
                live_response, live_error = self._place_limit_order(order, exit_side, price, reason, self.request, quantity=exit_qty)
                order["exit_order_response"] = live_response
                order["exit_order_id"] = self._order_id(live_response)
                order["exit_order_price"] = price
                order["exit_remarks"] = live_error or _broker_message(live_response)
            
                if live_error:
                    order["quote_error"] = live_error
                    order["last_exit_error"] = live_error
                    return False
                if order["exit_order_id"]:
                    order["status"] = "Exit_Pending"
                    order["pending_exit_reason"] = reason
                    return True
                return False
        
        self._mark_order_closed(order, reason, now, ltp)
        return True

    def _mark_order_closed(self, order: dict[str, Any], reason: str, now: datetime, ltp: float | None = None, remaining_qty: int | None = None) -> None:
        """Commit an order status locally as 'Closed', mapping PnL calculations."""
        if ltp is None:
            ltp = order.get("option_ltp")
        direction = 1 if order["side"] == "BUY" else -1
        order["option_ltp"] = ltp
        order["option_exit"] = ltp
        order["exit_time"] = now.isoformat(timespec="seconds")
        order["status"] = "Closed"
        order["exit_reason"] = reason
    
        if not order.get("live_trade") and order.get("entry_remarks") == "Paper entry active":
            order["entry_remarks"] = "Paper entry closed"
        if not order.get("live_trade"):
            order["exit_remarks"] = reason
        calc_qty = order["quantity"] if remaining_qty is None else remaining_qty
        order["pnl"] = (float(ltp) - float(order["option_entry"])) * direction * calc_qty + order.get("realized_pnl", 0.0)

    def _log_orders_to_csv(self) -> None:
        """Log final status orders to orders.csv and keep only the last 30 days of entries."""
        csv_path = BASE_DIR / "orders.csv"
    
        final_orders = [o for o in self.paper_orders if o.get("status") in FINAL_ORDER_STATUSES]
        if not final_orders:
            return
            
        # Initialize in-memory cache of logged keys if not present
        if not hasattr(self, '_logged_order_keys') or self._logged_order_keys is None:
            self._logged_order_keys = set()
            if csv_path.exists():
                try:
                    with open(csv_path, mode="r", newline="", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            okey = row.get("order_key")
                            if okey:
                                self._logged_order_keys.add(okey)
                except Exception as e:
                    print(f"Error initializing logged keys cache: {e}", flush=True)

        # Check if there are any new final orders that haven't been cached/logged yet
        has_new_final_order = False
        for o in final_orders:
            okey = self._order_row_key(o)
            if okey not in self._logged_order_keys:
                has_new_final_order = True
                break
        
        # If no new final order exists and CSV exists, bypass disk I/O completely
        if not has_new_final_order and csv_path.exists():
            return
        
        existing_rows = []
        existing_keys = set()
    
        thirty_days_ago = datetime.now() - timedelta(days=30)
    
        if csv_path.exists():
            try:
                with open(csv_path, mode="r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        entry_time_str = row.get("entry_time") or row.get("time") or ""
                        keep = True
                        if entry_time_str:
                            try:
                                entry_dt = datetime.fromisoformat(entry_time_str)
                                if entry_dt < thirty_days_ago:
                                    keep = False
                            except ValueError:
                                pass
                    
                        if keep:
                            existing_rows.append(row)
                            order_key = row.get("order_key")
                            if order_key:
                                existing_keys.add(order_key)
            except Exception as e:
                print(f"Error reading CSV file: {e}", flush=True)

        new_added = False
        headers = [
            "order_key",
            "trading_date",
            "entry_time",
            "exit_time",
            "underlying",
            "instrument_type",
            "tradingsymbol",
            "side",
            "quantity",
            "option_entry",
            "option_exit",
            "pnl",
            "status",
            "exit_reason",
            "entry_remarks",
            "exit_remarks",
            "trade_mode"
        ]

        for o in final_orders:
            okey = self._order_row_key(o)
            if okey not in existing_keys:
                entry_time_str = o.get("entry_time") or o.get("time") or ""
                keep = True
                if entry_time_str:
                    try:
                        entry_dt = datetime.fromisoformat(entry_time_str)
                        if entry_dt < thirty_days_ago:
                            keep = False
                    except ValueError:
                        pass
                if not keep:
                    continue

                row = {
                    "order_key": okey,
                    "trading_date": self.trading_date or datetime.now().date().isoformat(),
                    "entry_time": o.get("entry_time") or o.get("time"),
                    "exit_time": o.get("exit_time"),
                    "underlying": o.get("underlying"),
                    "instrument_type": o.get("instrument_type"),
                    "tradingsymbol": o.get("tradingsymbol"),
                    "side": o.get("side"),
                    "quantity": o.get("quantity"),
                    "option_entry": o.get("option_entry"),
                    "option_exit": o.get("option_exit"),
                    "pnl": o.get("pnl"),
                    "status": o.get("status"),
                    "exit_reason": o.get("exit_reason"),
                    "entry_remarks": o.get("entry_remarks"),
                    "exit_remarks": o.get("exit_remarks"),
                    "trade_mode": "REAL" if o.get("live_trade") else "PAPER"
                }
                existing_rows.append(row)
                existing_keys.add(okey)
                new_added = True

        if new_added or not csv_path.exists():
            try:
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=headers)
                    writer.writeheader()
                    for r in existing_rows:
                        row_to_write = {k: r.get(k) for k in headers}
                        writer.writerow(row_to_write)
                print(f"Logged/updated CSV file at {csv_path}. Total rows: {len(existing_rows)}", flush=True)
                self._logged_order_keys = existing_keys
            except Exception as e:
                print(f"Error writing CSV file: {e}", flush=True)
