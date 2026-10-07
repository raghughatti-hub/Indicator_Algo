"""Persistent execution instructions with immutable adverse-price boundaries."""
import math
import threading
from utils.clock import market_now, market_time


class LimitExecutionMixin:
    @staticmethod
    def _protective_reason(reason):
        return reason in {'SL','TSL','DAILY_LIMIT','SESSION_EXIT','MANUAL_EXIT','OPPOSITE_SIGNAL'} or reason.startswith('STRATEGY_')

    def _ensure_intent(self, order, purpose, side, reference, reason=None):
        key='entry_intent' if purpose=='ENTRY' else 'exit_intent'
        if order.get(key):
            return order[key]
        req=self.request
        protective=purpose=='EXIT' and self._protective_reason(reason or '')
        # Premium stops have an exact traded-price reference. Spot/strategy triggers
        # cannot be converted into a premium price, so freeze the first exit quote.
        if protective and reason in {'SL','TSL'} and req.sltp_instrument=='option':
            reference=float(order['stoploss'])
        prefix='entry' if purpose=='ENTRY' else ('protective_exit' if protective else 'exit')
        pct=getattr(req,prefix+'_slippage_pct')
        points=getattr(req,prefix+'_slippage_points')
        allowance=reference*pct/100 if points is None else points
        tick=order.get('tick_size') or .05
        boundary=reference+allowance if side=='BUY' else reference-allowance
        if boundary<=0:
            raise ValueError('Slippage allowance makes execution boundary nonpositive')
        boundary=self._round_to_tick(boundary,tick,'SELL' if side=='BUY' else 'BUY')
        intent=dict(purpose=purpose,side=side,reference=float(reference),boundary=boundary,
                    created_at=market_now().isoformat(),reason=reason,attempts=0,active=True,
                    state='READY',last_managed_at=None)
        order[key]=intent
        self._save_state()
        return intent

    def _intent_price(self, order, intent, quote):
        bid,ask=quote.get('best_buy'),quote.get('best_sell')
        if not bid or not ask or not all(math.isfinite(float(p)) and float(p)>0 for p in (bid,ask)) or bid>ask:
            return None,'Fresh two-sided bid/ask unavailable'
        spread=(ask-bid)/((ask+bid)/2)*100
        if intent['purpose']=='ENTRY' and spread>self.request.max_entry_spread_pct:
            return None,'Entry spread exceeds configured maximum'
        crossing=ask if intent['side']=='BUY' else bid
        inside=crossing<=intent['boundary'] if intent['side']=='BUY' else crossing>=intent['boundary']
        if not inside:
            return None,'PRICE_BOUNDARY_REACHED'
        # A marketable limit at the fixed boundary permits best-price execution
        # without repeated cancel/replaces for every tick of movement.
        return intent['boundary'],None

    def _start_execution_manager(self, order):
        if not self.request.continuous_execution or not order.get('live_trade'):
            return
        with self.order_lock:
            if order.get('_management_active') or order.get('chasing_active') or order.get('submission_unknown'):
                return
            intent=order.get('exit_intent') or order.get('entry_intent')
            if not intent or not intent.get('active'):
                return
            last=intent.get('last_managed_at')
            if last and (market_now()-market_time(last)).total_seconds()<self.request.execution_reprice_seconds:
                return
            order['_management_active']=True
        threading.Thread(target=self._execution_worker,args=(order,),daemon=True).start()

    def _execution_worker(self, order):
        try:
            self._manage_execution(order)
        except Exception as exc:
            with self.order_lock:
                self.entries_enabled=False
                self.last_order_error=f'Execution management failed: {type(exc).__name__}; reconcile tracked orders'
        finally:
            with self.order_lock:
                order['_management_active']=False
                self._save_state()

    def _manage_execution(self, order):
        """Broker I/O runs in a reserved worker; no blind replacement is permitted."""
        with self.order_lock:
            intent=order.get('exit_intent') or order.get('entry_intent')
            if not intent or not intent.get('active') or order.get('submission_unknown'):
                return
            purpose=intent['purpose']
            if purpose=='EXIT' and order.get('broker_net_qty') is not None and order['broker_net_qty']<int(order.get('quantity',0)):
                self.entries_enabled=False
                intent['state']='POSITION_RECONCILIATION_REQUIRED'
                order['execution_alert']='Broker/local quantity mismatch; position remains open'
                return
            intent['last_managed_at']=market_now().isoformat()
            if purpose=='EXIT' and int(order.get('quantity',0))==0 and not self._unsettled(order):
                intent.update(active=False,state='FILLED')
                return
            expired=purpose=='ENTRY' and ((market_now()-market_time(intent['created_at'])).total_seconds()>=self.request.entry_timeout_seconds or not self.entries_enabled or order.get('exit_intent'))
            records=list(self._unsettled(order,purpose))
        if purpose=='EXIT':
            with self.order_lock:
                if order.get('manual_submission') or order.get('submission_attempt'):
                    return
                other_records=[r for r in self._unsettled(order) if r['purpose']!='EXIT']
            for record in other_records:
                if not self._cancel_confirmed(order,record):
                    return
            with self.order_lock:
                if order.get('entry_intent'):
                    order['entry_intent'].update(active=False,state='EXIT_REQUESTED')
        # Apply fresh cumulative fills before deciding remaining quantity.
        for record in records:
            status=self._broker_order_status(record['order_id'])
            with self.order_lock:
                self._apply_broker_status(order,record,status)
        with self.order_lock:
            if order.get('submission_unknown') or order.get('fill_reconciliation_required'):
                return
            records=list(self._unsettled(order,purpose))
            if purpose=='ENTRY' and not records and int(order.get('quantity',0))>=int(order.get('requested_quantity',0)):
                intent.update(active=False,state='FILLED')
                return
        with self.order_lock:
            if purpose=='EXIT' and int(order.get('quantity',0))==0 and not self._unsettled(order):
                intent.update(active=False,state='FILLED')
                self._refresh_execution_state(order)
                return
        if expired:
            price,problem=None,None
        else:
            quote,error=self._quote_snapshot(order)
            price,problem=self._intent_price(order,intent,quote) if quote and not error else (None,error or 'Fresh quote unavailable')
        if expired or (purpose=='ENTRY' and problem=='PRICE_BOUNDARY_REACHED'):
            for record in records:
                if not self._cancel_confirmed(order,record):
                    return
            with self.order_lock:
                intent.update(active=False,state='EXPIRED' if expired else 'PRICE_BOUNDARY_REACHED')
                order['execution_alert']='Unfilled entry remainder cancelled; filled quantity remains protected'
                self._refresh_execution_state(order)
            return
        if problem:
            with self.order_lock:
                intent['state']=problem
                order['execution_alert']=problem
                if purpose=='EXIT':
                    self.entries_enabled=False
                    self.last_order_error='EXIT UNFILLED: '+problem+'; position remains open'
            # At an exit boundary retain the outstanding limit. Never widen it.
            return
        if records and all(abs(r['price']-price)<1e-9 for r in records):
            with self.order_lock:
                intent['state']='WORKING'
            return
        for record in records:
            if not self._cancel_confirmed(order,record):
                return
        with self.order_lock:
            if order.get('submission_unknown') or self._unsettled(order):
                return
            if purpose=='ENTRY':
                if not self.entries_enabled or order.get('exit_intent'):
                    return
                remaining=int(order['requested_quantity'])-int(order.get('quantity',0))
            else:
                remaining=int(order.get('quantity',0))
            if remaining<=0:
                intent.update(active=False,state='FILLED')
                return
            if intent['attempts']>=self.request.execution_max_attempts:
                intent['state']='RETRY_LIMIT_REACHED'
                order['execution_alert']='Retry limit reached; check broker terminal'
                self.entries_enabled=False
                return
            # Reservation stops user actions/other workers while submission is in flight.
            order['status']='Entry_Submitting' if purpose=='ENTRY' else 'Exit_Submitting'
            intent['attempts']+=1
            intent['state']='SUBMITTING'
            self._save_state()
        record=self._submit(order,intent['side'],price,purpose,self.request,remaining)
        with self.order_lock:
            intent['state']='WORKING' if record else ('UNKNOWN' if order.get('submission_unknown') else 'REJECTED')
            if record:
                order.pop('execution_alert',None)
