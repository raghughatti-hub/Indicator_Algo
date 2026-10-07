import csv
import math
import threading
import time as monotonic_time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from utils.clock import market_now, market_time
from models.schemas import IntradayRunRequest
from config.settings import BASE_DIR
from utils.helpers import (
    _broker_avg_price, _broker_filled_qty, _broker_message, _broker_order_id,
    _broker_value, _clean_order_id, _contract_name, _flatten_broker_orders,
    PRODUCT_TYPE_MAP, ACTIVE_ORDER_STATUSES, OPEN_ORDER_STATUSES, FINAL_ORDER_STATUSES,
)


from execution.limit_execution import LimitExecutionMixin


class OrderManagerMixin(LimitExecutionMixin):
    """Confirmed-fill ledger. Unknown submissions never authorize a replacement."""
    _BROKER_SYNC_INTERVAL = 2.0

    def _save_state(self):
        if hasattr(self, "_persist_state"):
            self._persist_state()

    def _entry_waits_for_blank_limit(self, request):
        return request.entry_order_mode in {"Limit_Below", "Limit_Above"} and request.entry_limit_price is None

    @staticmethod
    def _round_to_tick(price, tick, side):
        if not math.isfinite(float(price)) or float(price) <= 0 or not math.isfinite(float(tick)) or float(tick) <= 0:
            raise ValueError("Price and tick must be positive finite numbers")
        px, step = Decimal(str(price)), Decimal(str(tick))
        mode = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
        result = (px / step).to_integral_value(rounding=mode) * step
        return float(max(step, result))

    def _entry_limit_price(self, order, request, quote_result=None):
        quote, error = quote_result if quote_result is not None else self._quote_snapshot(order)
        if not quote:
            return None, error
        ltp = quote.get("ltp")
        tick = float(order.get("tick_size") or quote.get("tick_size") or .05)
        order["option_ltp"] = ltp if ltp is not None else order.get("option_ltp")
        if request.entry_order_mode == "Aggressive_Entry":
            raw = quote.get("best_sell" if order["side"] == "BUY" else "best_buy") or ltp
            return (self._round_to_tick(raw, tick, order["side"]), None) if raw else (None, "Fresh bid/ask unavailable")
        if request.entry_order_mode == "True_Limit_LTP":
            return (self._round_to_tick(ltp, tick, order["side"]), None) if ltp else (None, "Fresh LTP unavailable")
        trigger = request.entry_limit_price
        if trigger is None or ltp is None:
            return None, None
        if request.entry_order_mode == "Limit_Below" and ltp > trigger:
            return None, None
        if request.entry_order_mode == "Limit_Above" and ltp < trigger:
            return None, None
        return self._round_to_tick(trigger, tick, order["side"]), None

    def _exit_limit_price(self, order, request, exit_side):
        if request.exit_order_mode == "False":
            return None, "Exit mode is disabled"
        quote, error = self._quote_snapshot(order)
        if not quote:
            return None, error
        ltp = quote.get("ltp")
        tick = float(order.get("tick_size") or quote.get("tick_size") or .05)
        if request.exit_order_mode == "Aggressive_Exit":
            raw = quote.get("best_buy" if exit_side == "SELL" else "best_sell") or ltp
            return (self._round_to_tick(raw, tick, exit_side), None) if raw else (None, "Fresh bid/ask unavailable")
        if request.exit_order_mode == "True_Limit_LTP":
            return (self._round_to_tick(ltp, tick, exit_side), None) if ltp else (None, "Fresh LTP unavailable")
        trigger = request.exit_limit_price
        if trigger is None:
            return None, "Conditional exit requires an explicit limit price"
        if ltp is None or (request.exit_order_mode == "Limit_Below" and ltp > trigger) or (request.exit_order_mode == "Limit_Above" and ltp < trigger):
            return None, None
        return self._round_to_tick(trigger, tick, exit_side), None

    def _validate_order_size(self, order, quantity):
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        contract = order.get("trade_contract") or {}
        lot = contract.get("lot_size")
        if order.get("instrument_type") != "Spot":
            if not lot or not math.isfinite(float(lot)) or int(lot) <= 0:
                raise ValueError("Verified contract lot size is required for derivatives")
            if quantity % int(lot):
                raise ValueError(f"Quantity must be a multiple of lot size {int(lot)}")

    def _unsettled(self, order, purpose=None):
        return [r for r in order.get("broker_orders", []) if not r.get("terminal") and (purpose is None or r["purpose"] == purpose)]

    def _unknown(self, order, message):
        order["status"] = "Recovery_Required"
        order["submission_unknown"] = True
        order["last_exit_error"] = message
        self.entries_enabled = False
        self.last_order_error = message
        self._save_state()

    def _register(self, order, oid, purpose, side, quantity, price):
        existing = next((r for r in order.get("broker_orders", []) if r["order_id"] == str(oid)), None)
        if existing:
            if (existing["purpose"], existing["side"], existing["quantity"]) != (purpose, side, int(quantity)):
                raise RuntimeError("Broker order ID conflicts with saved execution intent")
            return existing
        record = dict(order_id=str(oid),purpose=purpose,side=side,quantity=int(quantity),price=float(price),filled=0,cost=0.,terminal=False)
        order.setdefault("broker_orders", []).append(record)
        if purpose != "MANUAL":
            order["entry_order_id" if purpose == "ENTRY" else "exit_order_id"] = str(oid)
        self._save_state()
        return record

    def _apply_broker_status(self, order, record, status):
        state = self._mapped_broker_state(status)
        if state == "unknown":
            return state
        filled = _broker_filled_qty(status)
        if state == "filled" and filled == 0:
            # COMPLETE is the broker's assertion that the accepted order fully filled.
            filled = record["quantity"]
        if filled < record["filled"] or filled > record["quantity"]:
            self._unknown(order, "Inconsistent broker fill quantity; reconcile before further orders")
            return "unknown"
        avg = _broker_avg_price(status)
        if filled > record["filled"]:
            if avg is None or not math.isfinite(avg) or avg <= 0:
                order["fill_reconciliation_required"]=True
                self.entries_enabled=False
                self.last_order_error = "Broker reported a fill without its price; awaiting reconciliation"
                return "unknown"
            order.pop("fill_reconciliation_required",None)
            cost = filled * avg
            delta = filled - record["filled"]
            price = (cost - record["cost"]) / delta
            record["filled"], record["cost"] = filled, cost
            if record["purpose"] == "ENTRY":
                old_qty = int(order.get("quantity", 0))
                order["option_entry"] = ((order.get("option_entry", 0) or 0) * old_qty + delta * price) / (old_qty + delta)
                order["quantity"] = old_qty + delta
                self._reset_risk_levels(order, self.request)
            else:
                self._apply_manual_trade_fill(order, record["side"], delta, price)
        if state in {"filled", "cancelled", "rejected"}:
            record["terminal"] = True
            record["terminal_state"] = state
        self._refresh_execution_state(order)
        self._save_state()
        return state

    def _refresh_execution_state(self, order):
        if order.get("submission_unknown"):
            return
        pending = self._unsettled(order)
        if any(r["purpose"] == "EXIT" for r in pending):
            order["status"] = "Exit_Pending"
        elif any(r["purpose"] == "ENTRY" for r in pending):
            order["status"] = "Entry_Pending"
        elif int(order.get("quantity", 0)) > 0:
            order["status"] = "Active"
            order["exit_order_id"] = None
        elif pending:
            order["status"] = "Exit_Pending"
        elif any(r.get("filled", 0) for r in order.get("broker_orders", [])):
            self._mark_order_closed(order, order.get("pending_exit_reason") or "FILLED_EXIT", market_now(), order.get("option_ltp"), remaining_qty=0)
        else:
            order["status"] = "Entry_Rejected"

    def _cancel_confirmed(self, order, record):
        try:
            self.zebu.cancel_order(record["order_id"])
        except Exception:
            pass  # A failed cancel is never proof of cancellation.
        for _ in range(10):
            status = self._broker_order_status(record["order_id"])
            with self.order_lock:
                self._apply_broker_status(order, record, status)
            if record["terminal"]:
                return True
            monotonic_time.sleep(.1)
        order["last_exit_error"] = "Cancellation unconfirmed; original order remains tracked"
        self._save_state()
        return False

    def _process_entry_order(self, order, request, quote_result=None):
        with self.order_lock:
            if order.get("status") != "Idle" or not self.entries_enabled:
                return
            now = market_now()
            from utils.helpers import _parse_clock, _is_market_time, NSE_OPEN, NSE_CLOSE
            if not _is_market_time(now, _parse_clock(request.start_time,NSE_OPEN), _parse_clock(request.exit_time,NSE_CLOSE)):
                order["status"] = "Entry_Rejected"
                order["entry_remarks"] = "Entry window closed"
                self._save_state()
                return
            contract = order.get("trade_contract")
            if not contract or contract.get("error"):
                order["quote_error"] = "Exact contract unavailable; no order submitted"
                return
            quantity = int(order.get("requested_quantity", order["quantity"]))
            try:
                self._validate_order_size(order,quantity)
            except ValueError as exc:
                order["status"] = "Entry_Rejected"
                order["entry_remarks"] = str(exc)
                self._save_state()
                return
            price, error = self._entry_limit_price(order,request,quote_result=quote_result)
            if price is None:
                if error:
                    order["quote_error"] = error
                    order["quote_retry_count"] = order.get("quote_retry_count",0)+1
                else:
                    order["entry_remarks"] = "Waiting for conditional price trigger"
                    order["quote_error"] = None
                return
            if error:
                order["quote_error"] = error
                return
            if not request.live_trade:
                quote, error = quote_result if quote_result is not None else self._quote_snapshot(order)
                if request.continuous_execution:
                    intent=self._ensure_intent(order,"ENTRY",order["side"],price)
                    if (market_now()-market_time(intent["created_at"])).total_seconds()>=request.entry_timeout_seconds:
                        intent.update(active=False,state="EXPIRED")
                        order.update(status="Entry_Rejected",quantity=0,entry_remarks="Paper entry expired")
                        self._save_state()
                        return
                    price,problem=self._intent_price(order,intent,quote or {})
                    if problem:
                        intent["state"]=problem
                        order["execution_alert"]=problem
                        if problem=="PRICE_BOUNDARY_REACHED":
                            intent["active"]=False
                            order.update(status="Entry_Rejected",quantity=0)
                        self._save_state()
                        return
                    intent.update(active=False,state="FILLED")
                crossing = (quote or {}).get("best_sell" if order["side"] == "BUY" else "best_buy") or (quote or {}).get("ltp")
                if not crossing or (order["side"] == "BUY" and price < crossing) or (order["side"] == "SELL" and price > crossing):
                    order["entry_remarks"] = "Paper limit is not marketable; awaiting fill"
                    return
                order.update(status="Active",option_entry=float(crossing),option_ltp=float(crossing),entry_order_price=price,entry_remarks="Paper entry filled")
                self._reset_risk_levels(order,request)
                self._save_state()
                return
            import uuid
            order.update(status="Entry_Submitting",requested_quantity=quantity,quantity=0,paper=False,entry_order_price=price,chase_batch=str(uuid.uuid4()))
            self._save_state()
        if request.continuous_execution:
            self._ensure_intent(order,"ENTRY",order["side"],price)
            self._start_execution_manager(order)
        elif request.enable_price_chasing:
            order["chasing_active"] = True
            threading.Thread(target=self._run_price_chasing_loop,args=(order,order["side"],price,"ENTRY",request,quantity,True),daemon=True).start()
        else:
            self._submit(order,order["side"],price,"ENTRY",request,quantity)

    def _submit(self, order, side, price, purpose, request, quantity):
        with self.order_lock:
            if (purpose == "ENTRY" or (purpose == "MANUAL" and side == order["side"])) and not self.entries_enabled:
                order["status"] = "Active" if order.get("quantity",0)>0 else "Entry_Rejected"
                self._save_state()
                return None
            import uuid
            tag = "algo" + uuid.uuid4().hex
            order["submission_attempt"] = dict(tag=tag,purpose=purpose,side=side,quantity=quantity,price=price)
            self._save_state()
        response,error = self._place_limit_order(order,side,price,purpose,request,quantity)
        with self.order_lock:
            if not response:
                self._unknown(order,error or "Order submission outcome unknown")
                return None
            oid = self._order_id(response)
            if not oid:
                if response.get("paper") or response.get("stat") == "Not_Ok" or response.get("status") == "error":
                    order.pop("submission_attempt", None)
                    order["status"] = "Active" if order.get("quantity",0)>0 else "Entry_Rejected"
                    order["entry_remarks" if purpose=="ENTRY" else "exit_remarks"] = error or _broker_message(response)
                    self._save_state()
                else:
                    self._unknown(order,"Broker response lacks order ID; reconcile submission")
                return None
            record=self._register(order,oid,purpose,side,quantity,price)
            record["tag"]=tag
            order.pop("submission_attempt",None)
            self._refresh_execution_state(order)
            self._save_state()
            return record

    def _place_limit_order(self, order, side, price, reason, request, quantity=None):
        try:
            qty = int(order["quantity"]) if quantity is None else int(quantity)
            try:
                self._validate_order_size(order,qty)
            except ValueError as exc:
                return {"stat":"Not_Ok","emsg":str(exc)},str(exc)
            response=self.zebu.place_order(exchange=order["trade_contract"]["exchange"],tradingsymbol=order["tradingsymbol"],side=side,quantity=qty,product_type=PRODUCT_TYPE_MAP[request.order_product_type],price_type="LMT",price=price,trigger_price=0,confirm_live=True,client_order_id=(order.get("submission_attempt") or {}).get("tag"))
            if response and response.get("paper"):
                return response,response.get("message","Live order blocked")
            if response and response.get("stat")=="Not_Ok":
                return response,response.get("emsg","Broker rejected order")
            return response,None
        except Exception as exc:
            return None, f"{reason} submission outcome unknown: {type(exc).__name__}"

    def _place_final_limit_order(self, order, side, reason, request, quantity):
        # A bounded, marketable limit is safer than an unbounded +/-10% sweep.
        quote,error=self._quote_snapshot(order)
        raw=(quote or {}).get("best_sell" if side=="BUY" else "best_buy") or (quote or {}).get("ltp")
        if not raw:
            return {"stat":"Not_Ok","emsg":"Fresh quote unavailable"},error
        anchor=order.get("entry_order_price") if reason=="ENTRY" else order.get("exit_order_price")
        anchor=anchor or raw
        cap=anchor*(1+request.chase_slippage_pct/100) if side=="BUY" else anchor*(1-request.chase_slippage_pct/100)
        if (side=="BUY" and raw>cap) or (side=="SELL" and raw<cap):
            return {"stat":"Not_Ok","emsg":"Slippage cap prevents marketable sweep"},"Slippage cap reached"
        return self._place_limit_order(order,side,self._round_to_tick(raw,order.get("tick_size") or .05,side),reason,request,quantity)

    def _run_price_chasing_loop(self, order, side, initial_price, reason, request, total_quantity, is_entry):
        purpose="ENTRY" if is_entry else "EXIT"
        deadline=monotonic_time.monotonic()+request.chase_timeout_seconds
        attempts=0
        record=None
        try:
            while attempts<request.chase_max_retries and monotonic_time.monotonic()<deadline:
                if order.get("submission_unknown"):
                    return
                if order.get("abort_chasing") or (is_entry and not self.entries_enabled):
                    break
                filled=sum(r["filled"] for r in order.get("broker_orders",[]) if r["purpose"]==purpose and r.get("batch")==order.get("chase_batch"))
                remaining=total_quantity-filled
                if remaining<=0:
                    break
                price,error=(self._entry_limit_price(order,request) if is_entry else self._exit_limit_price(order,request,side))
                if price is None or error:
                    break
                cap=initial_price*(1+request.chase_slippage_pct/100) if side=="BUY" else initial_price*(1-request.chase_slippage_pct/100)
                tick=order.get("tick_size") or .05
                cap=self._round_to_tick(cap,tick,"SELL" if side=="BUY" else "BUY")
                price=min(price,cap) if side=="BUY" else max(price,cap)
                record=self._submit(order,side,self._round_to_tick(price,order.get("tick_size") or .05,side),purpose,request,remaining)
                attempts+=1
                if record is None:
                    return
                record["batch"]=order.get("chase_batch")
                monotonic_time.sleep(.2)
                with self.order_lock:
                    self._apply_broker_status(order,record,self._broker_order_status(record["order_id"]))
                    if not record["terminal"] and not self._cancel_confirmed(order,record):
                        return  # Still live/unknown: never replace or sweep.
            if record and not record["terminal"]:
                with self.order_lock:
                    if not self._cancel_confirmed(order,record):
                        return
            if request.chase_sweep_market and not order.get("abort_chasing") and not order.get("submission_unknown") and (not is_entry or self.entries_enabled):
                filled=sum(r["filled"] for r in order.get("broker_orders",[]) if r["purpose"]==purpose and r.get("batch")==order.get("chase_batch"))
                remaining=total_quantity-filled
                if remaining>0:
                    # Keep the final limit in the ledger until a terminal status is confirmed.
                    quote,error=(self._entry_limit_price(order,request) if is_entry else self._exit_limit_price(order,request,side))
                    cap=initial_price*(1+request.chase_slippage_pct/100) if side=="BUY" else initial_price*(1-request.chase_slippage_pct/100)
                    if quote and not error and ((side=="BUY" and quote<=cap) or (side=="SELL" and quote>=cap)):
                        record=self._submit(order,side,quote,purpose,request,remaining)
                        if record:
                            record["batch"]=order.get("chase_batch")
        except Exception as exc:
            with self.order_lock:
                self.last_order_error=f"Chasing stopped: {type(exc).__name__}; reconcile tracked order"
        finally:
            with self.order_lock:
                order["chasing_active"]=False
                self._refresh_execution_state(order)
                self._save_state()

    def _consume_order_updates(self, order):
        take=getattr(self.zebu,"take_order_update",None)
        if not callable(take):
            return
        for record in list(self._unsettled(order)):
            update=take(record["order_id"])
            if update and _broker_filled_qty(update)>=record.get("filled",0):
                self._apply_broker_status(order,record,update)

    def _sync_broker_order_state(self, order):
        if not order.get("live_trade") or order.get("chasing_active") or order.get("_management_active"):
            return
        if self.request and self.request.continuous_execution and any(order.get(k,{}).get("active") for k in ("entry_intent","exit_intent")):
            return
        now=monotonic_time.monotonic()
        if now-order.get("_broker_sync_at",0)<self._BROKER_SYNC_INTERVAL:
            return
        order["_broker_sync_at"]=now
        # Preserve and migrate saved IDs from earlier versions instead of losing them.
        if not order.get("broker_orders"):
            if order.get("entry_order_id") and order.get("status")=="Entry_Pending":
                qty=int(order.get("requested_quantity",order.get("quantity",0)))
                order["requested_quantity"]=qty
                order["quantity"]=0
                self._register(order,order["entry_order_id"],"ENTRY",order["side"],qty,order.get("entry_order_price") or order.get("option_entry") or 0)
            elif order.get("exit_order_id") and order.get("status")=="Exit_Pending":
                self._register(order,order["exit_order_id"],"EXIT","SELL" if order["side"]=="BUY" else "BUY",order["quantity"],order.get("exit_order_price") or order.get("option_ltp") or 0)
        for record in list(self._unsettled(order)):
            self._apply_broker_status(order,record,self._broker_order_status(record["order_id"]))

    def reconcile_submissions(self):
        """Adopt only an exact client-tag match; absence remains unknown."""
        with self.order_lock:
            book=self.zebu.get_order_book()
            if not isinstance(book,list):
                raise RuntimeError("Broker order book unavailable")
            for order in self.paper_orders:
                attempt=order.get("submission_attempt")
                if not order.get("submission_unknown") or not attempt:
                    continue
                matches=[row for row in book if (row.get("remarks") or row.get("tag"))==attempt["tag"] and self._order_id(row)]
                if len(matches)!=1:
                    continue
                row=matches[0]
                order["submission_unknown"]=False
                order["manual_submission"]=False
                record=self._register(order,self._order_id(row),attempt["purpose"],attempt["side"],attempt["quantity"],attempt["price"])
                order.pop("submission_attempt",None)
                self._apply_broker_status(order,record,row)
                self._refresh_execution_state(order)
            self._save_state()
            return self.orders_status()

    def _broker_order_status(self, order_id):
        try:
            history=self.zebu.single_order_history(order_id)
            result=self._best_broker_status(_flatten_broker_orders(history),_clean_order_id(order_id))
            if result is not None:
                return result
        except Exception:
            pass
        try:
            result=self._best_broker_status(_flatten_broker_orders(self.zebu.get_order_book()),_clean_order_id(order_id))
            return result
        except Exception:
            return None

    @staticmethod
    def _best_broker_status(rows, order_id):
        # SDK histories arrive newest first. Never rank an older fill over later cancellation.
        matches=[r for r in rows if not order_id or _broker_order_id(r)==order_id]
        return matches[0] if matches else None

    @staticmethod
    def _mapped_broker_state(broker):
        if not isinstance(broker,dict):
            return "unknown"
        status=str(_broker_value(broker,("status","order_status","ordstatus","ord_status")) or "").upper().strip()
        if status in {"COMPLETE","FILLED","TRADED","EXECUTED"}:
            return "filled"
        if status in {"CANCELLED","CANCELED"}:
            return "cancelled"
        if status in {"REJECTED","FAILED","REJECT"}:
            return "rejected"
        if status in {"OPEN","PENDING","TRIGGER_PENDING","TRIGGER PENDING","PARTIALLY FILLED","PARTIAL","VALIDATION PENDING","PUT ORDER REQ RECEIVED"}:
            return "pending"
        # An API error or a message containing 'filled' is not an order status.
        return "unknown"

    @staticmethod
    def _order_id(response):
        return _broker_order_id(response)

    def _close_open_paper_orders(self, reason, now):
        for order in list(self.paper_orders):
            if order.get("status") in OPEN_ORDER_STATUSES:
                self._close_order(order,reason,now)
        self.day_pnl=sum(float(o.get("pnl") or 0) for o in self.paper_orders)

    def _close_order(self, order, reason, now, ltp=None):
        with self.order_lock:
            if self.request and self.request.continuous_execution and order.get("live_trade") and order.get("status") in OPEN_ORDER_STATUSES:
                if order.get("status")=="Idle":
                    order.update(status="Entry_Rejected",entry_remarks="Unsubmitted entry cancelled")
                    self._save_state()
                    return True
                if order.get("broker_net_qty") is not None and order["broker_net_qty"]<int(order.get("quantity",0)):
                    order["last_exit_error"]="Broker/local quantity mismatch; reconcile exposure before exit"
                    self.entries_enabled=False
                    return False
                reference=order.get("option_ltp") or order.get("option_entry")
                if not reference:
                    order["last_exit_error"]="Exit reference unavailable"
                    return False
                self._ensure_intent(order,"EXIT","SELL" if order["side"]=="BUY" else "BUY",float(reference),reason)
                order["pending_exit_reason"]=reason
                self._start_execution_manager(order)
                return True
            if order.get("status") in {"Exit_Submitting","Exit_Pending"} or self._unsettled(order,"EXIT"):
                return True
            if order.get("submission_unknown") or order.get("manual_submission") or order.get("_management_active"):
                order["last_exit_error"]="Resolve outstanding submission before exit"
                return False
            if order.get("status") not in OPEN_ORDER_STATUSES:
                return False
            if order.get("chasing_active"):
                order["abort_chasing"] = True
                return False
            for record in list(self._unsettled(order)):
                if not self._cancel_confirmed(order,record):
                    return False
            if order.get("status")=="Idle":
                order.update(status="Entry_Rejected",entry_remarks="Unsubmitted entry cancelled")
                self._save_state()
                return True
            qty=int(order.get("quantity",0))
            if qty<=0:
                self._refresh_execution_state(order)
                return not self._unsettled(order)
            if not order.get("live_trade"):
                quote,error=self._quote_snapshot(order)
                side="SELL" if order["side"]=="BUY" else "BUY"
                price=(quote or {}).get("best_buy" if side=="SELL" else "best_sell") or (quote or {}).get("ltp")
                if not price:
                    order["last_exit_error"]=error or "Fresh exit price unavailable"
                    return False
                if self.request and self.request.continuous_execution:
                    intent=self._ensure_intent(order,"EXIT",side,float(price),reason)
                    _,problem=self._intent_price(order,intent,quote)
                    if problem:
                        intent["state"]=problem
                        order["execution_alert"]=problem
                        self.entries_enabled=False
                        self._save_state()
                        return False
                    intent.update(active=False,state="FILLED")
                self._apply_manual_trade_fill(order,side,qty,float(price))
                self._mark_order_closed(order,reason,now,float(price),remaining_qty=0)
                self._save_state()
                return True
            if not self.request:
                return False
            if order.get("broker_net_qty") is not None and order["broker_net_qty"]<qty:
                order["last_exit_error"]="Broker/local quantity mismatch; reconcile exposure before exit"
                self.entries_enabled=False
                return False
            side="SELL" if order["side"]=="BUY" else "BUY"
            exit_request = self.request.model_copy(update={"exit_order_mode":"Aggressive_Exit"}) if self._protective_reason(reason) else self.request
            price,error=self._exit_limit_price(order,exit_request,side)
            if price is None or error:
                order["last_exit_error"]=error or "Waiting for exit limit condition"
                return False
            order.update(status="Exit_Submitting",pending_exit_reason=reason,exit_order_price=price)
            self._save_state()
        if self.request.continuous_execution:
            self._ensure_intent(order,"EXIT",side,price,reason)
            self._start_execution_manager(order)
            return True
        if self.request.enable_price_chasing:
            import uuid
            order["chase_batch"]=str(uuid.uuid4())
            order["abort_chasing"]=False
            order["chasing_active"]=True
            threading.Thread(target=self._run_price_chasing_loop,args=(order,side,price,"EXIT",exit_request,qty,False),daemon=True).start()
            return True
        return self._submit(order,side,price,"EXIT",self.request,qty) is not None

    def _mark_order_closed(self, order, reason, now, ltp=None, remaining_qty=None):
        qty=int(order.get("quantity",0)) if remaining_qty is None else int(remaining_qty)
        if order.get("live_trade") and qty>0:
            # This path cannot turn real exposure into a display-only closure.
            raise RuntimeError("A live position closes only through confirmed fills")
        direction=1 if order["side"]=="BUY" else -1
        price=ltp if ltp is not None else order.get("option_ltp")
        order.update(option_exit=price,exit_time=market_time(now).isoformat(),status="Closed",exit_reason=reason)
        order["pnl"]=float(order.get("realized_pnl",0))+(float(price or 0)-float(order.get("option_entry") or 0))*direction*qty
        order["closed_quantity"]=sum(r.get("filled",0) for r in order.get("broker_orders",[]) if r.get("purpose")=="ENTRY") or order.get("requested_quantity") or qty
        order["quantity"]=0
        order["pending_manual_orders"]=[]
        order["exit_remarks"]=reason
        self._save_state()

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
    
        thirty_days_ago = market_now() - timedelta(days=30)
    
        if csv_path.exists():
            try:
                with open(csv_path, mode="r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        entry_time_str = row.get("entry_time") or row.get("time") or ""
                        keep = True
                        if entry_time_str:
                            try:
                                entry_dt = market_time(entry_time_str)
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
                        entry_dt = market_time(entry_time_str)
                        if entry_dt < thirty_days_ago:
                            keep = False
                    except ValueError:
                        pass
                if not keep:
                    continue

                row = {
                    "order_key": okey,
                    "trading_date": self.trading_date or market_now().date().isoformat(),
                    "entry_time": o.get("entry_time") or o.get("time"),
                    "exit_time": o.get("exit_time"),
                    "underlying": o.get("underlying"),
                    "instrument_type": o.get("instrument_type"),
                    "tradingsymbol": o.get("tradingsymbol"),
                    "side": o.get("side"),
                    "quantity": o.get("closed_quantity", o.get("quantity")),
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
