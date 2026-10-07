"""Continuous limit execution: no live broker calls or credentials."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import timedelta
from brokers.base import BaseBrokerClient
from brokers.upstox.client import UpstoxClient
from config.settings import Settings
from execution.runner import IntradayRunner
from models.schemas import IntradayRunRequest
from test_trading_safety import make_runner, make_order, NOW


def runner():
    r=make_runner()
    r.request=IntradayRunRequest(live_trade=True,entry_slippage_points=.3,exit_slippage_points=.3,protective_exit_slippage_points=.5)
    r._quote_snapshot=Mock(return_value=({'ltp':100,'best_buy':99.95,'best_sell':100.05},None))
    r._broker_order_status=Mock(return_value={'status':'OPEN','fillshares':'0'})
    r._start_execution_manager=Mock()
    return r

class LimitExecutionTests(unittest.TestCase):
    def test_entry_uses_fixed_marketable_boundary(self):
        r=runner();o=make_order(quantity=0,status='Entry_Submitting',requested_quantity=100)
        r._ensure_intent(o,'ENTRY','BUY',100)
        r._manage_execution(o)
        self.assertEqual(r.broker.place_order.call_args.kwargs['price'],100.3)
        self.assertEqual(r.broker.place_order.call_args.kwargs['price_type'],'LMT')
    def test_entry_boundary_is_not_reset_by_new_quote(self):
        r=runner();o=make_order(quantity=0,requested_quantity=100)
        first=r._ensure_intent(o,'ENTRY','BUY',100)
        self.assertIs(first,r._ensure_intent(o,'ENTRY','BUY',110))
        self.assertEqual(first['boundary'],100.3)
    def test_pending_boundary_limit_does_not_churn(self):
        r=runner();o=make_order(quantity=0,requested_quantity=100)
        r._ensure_intent(o,'ENTRY','BUY',100)
        r._register(o,'id','ENTRY','BUY',100,100.3)
        r._manage_execution(o)
        r.broker.place_order.assert_not_called();r.broker.cancel_order.assert_not_called()
    def test_entry_gap_cancels_only_unfilled_remainder(self):
        r=runner();o=make_order(quantity=0,requested_quantity=100)
        r._ensure_intent(o,'ENTRY','BUY',100)
        r._register(o,'id','ENTRY','BUY',100,100.3)
        r._broker_order_status.side_effect=[{'status':'OPEN','fillshares':'40','avgprc':'100'},{'status':'CANCELLED','fillshares':'40','avgprc':'100'}]
        r._quote_snapshot.return_value=({'ltp':101,'best_buy':100.9,'best_sell':101},None)
        r._manage_execution(o)
        self.assertEqual(o['quantity'],40);self.assertEqual(o['status'],'Active')
        self.assertFalse(o['entry_intent']['active']);r.broker.place_order.assert_not_called()
    def test_expired_entry_is_not_replaced(self):
        r=runner();o=make_order(quantity=0,requested_quantity=100)
        intent=r._ensure_intent(o,'ENTRY','BUY',100)
        intent['created_at']=(NOW-timedelta(seconds=20)).isoformat()
        with patch('execution.limit_execution.market_now',return_value=NOW):r._manage_execution(o)
        self.assertEqual(intent['state'],'EXPIRED');r.broker.place_order.assert_not_called()
    def test_gap_stop_preserves_boundary_and_exposure(self):
        r=runner();o=make_order(stoploss=95)
        intent=r._ensure_intent(o,'EXIT','SELL',93,'SL')
        r._quote_snapshot.return_value=({'ltp':93,'best_buy':93,'best_sell':93.1},None)
        r._manage_execution(o)
        self.assertEqual(intent['boundary'],94.5)
        self.assertEqual(intent['state'],'PRICE_BOUNDARY_REACHED')
        self.assertEqual(o['quantity'],100);self.assertFalse(r.entries_enabled)
        r.broker.place_order.assert_not_called()
    def test_existing_exit_is_retained_beyond_boundary(self):
        r=runner();o=make_order(stoploss=95)
        r._ensure_intent(o,'EXIT','SELL',95,'SL');r._register(o,'id','EXIT','SELL',100,94.5)
        r._quote_snapshot.return_value=({'ltp':93,'best_buy':93,'best_sell':93.1},None)
        r._manage_execution(o)
        r.broker.cancel_order.assert_not_called();r.broker.place_order.assert_not_called()
    def test_exit_recovery_in_price_does_not_clear_instruction(self):
        r=runner();o=make_order(stoploss=95)
        r._ensure_intent(o,'EXIT','SELL',95,'SL')
        r._manage_execution(o)
        self.assertEqual(r.broker.place_order.call_args.kwargs['price'],94.5)
        self.assertTrue(o['exit_intent']['active'])
    def test_partial_exit_replacement_uses_remaining_quantity(self):
        r=runner();o=make_order(stoploss=95)
        r._ensure_intent(o,'EXIT','SELL',95,'SL');r._register(o,'id','EXIT','SELL',100,96)
        r._broker_order_status.side_effect=[{'status':'OPEN','fillshares':'40','avgprc':'96'},{'status':'CANCELLED','fillshares':'40','avgprc':'96'}]
        r._manage_execution(o)
        self.assertEqual(r.broker.place_order.call_args.kwargs['quantity'],60)
        self.assertEqual(o['quantity'],60)
    def test_exit_cancels_entry_and_protects_late_fill(self):
        r=runner();o=make_order(quantity=0,requested_quantity=100,stoploss=95)
        r._ensure_intent(o,'ENTRY','BUY',100);r._register(o,'id','ENTRY','BUY',100,100.3)
        r._ensure_intent(o,'EXIT','SELL',95,'SL')
        r._broker_order_status.return_value={'status':'CANCELLED','fillshares':'70','avgprc':'100'}
        r._manage_execution(o)
        self.assertEqual(r.broker.place_order.call_args.kwargs['quantity'],70)
        self.assertEqual(r.broker.place_order.call_args.kwargs['side'],'SELL')
        self.assertFalse(o['entry_intent']['active'])
    def test_unconfirmed_cancel_never_duplicates_exit(self):
        r=runner();o=make_order(stoploss=95)
        r._ensure_intent(o,'EXIT','SELL',95,'SL');r._register(o,'id','EXIT','SELL',100,96)
        with patch('execution.order_manager.monotonic_time.sleep'):r._manage_execution(o)
        r.broker.place_order.assert_not_called()
    def test_wide_or_missing_book_blocks_entry(self):
        r=runner();o=make_order();intent=r._ensure_intent(o,'ENTRY','BUY',100)
        for book in ({'best_buy':98,'best_sell':100},{'ltp':100},{'best_buy':101,'best_sell':100}):
            price,error=r._intent_price(o,intent,book)
            self.assertIsNone(price);self.assertTrue(error)
    def test_sell_boundary_rounds_inward(self):
        r=runner();r.request.entry_slippage_points=.32;o=make_order(side='SELL')
        self.assertEqual(r._ensure_intent(o,'ENTRY','SELL',100)['boundary'],99.7)
    def test_strategy_stop_is_protective_and_latched(self):
        r=runner();r.request.exit_order_mode='Limit_Above';r.request.exit_limit_price=200
        o=make_order();r.paper_orders=[o]
        self.assertTrue(r._close_order(o,'STRATEGY_TSL',NOW))
        self.assertTrue(o['exit_intent']['active']);self.assertEqual(o['exit_intent']['boundary'],99.5)
    def test_retry_limit_does_not_widen_boundary(self):
        r=runner();o=make_order(stoploss=95)
        intent=r._ensure_intent(o,'EXIT','SELL',95,'SL');intent['attempts']=r.request.execution_max_attempts
        r._manage_execution(o)
        self.assertEqual(intent['state'],'RETRY_LIMIT_REACHED');self.assertEqual(intent['boundary'],94.5)
        r.broker.place_order.assert_not_called()
    def test_resume_is_blocked_with_latched_exit(self):
        r=runner();o=make_order();r.paper_orders=[o]
        r._ensure_intent(o,'EXIT','SELL',100,'SL')
        with self.assertRaisesRegex(RuntimeError,'pending exit'):r.start(r.request)
    def test_boundary_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            r=runner();r.state_path=Path(folder,'state.json');o=make_order();r.paper_orders=[o]
            r._ensure_intent(o,'EXIT','SELL',100,'SL')
            recovered=IntradayRunner(None,state_path=r.state_path)
            self.assertEqual(recovered.paper_orders[0]['exit_intent']['boundary'],79.5)
            self.assertTrue(recovered.paper_orders[0]['exit_intent']['active'])
    def test_order_events_apply_fill_without_polling(self):
        r=runner();b=BaseBrokerClient(Settings());r.broker.take_order_update=b.take_order_update
        o=make_order(quantity=0);r._register(o,'id','ENTRY','BUY',100,100)
        b.publish_order_update({'norenordno':'id','status':'OPEN','fillshares':'40','avgprc':'100'})
        r._consume_order_updates(o)
        self.assertEqual(o['quantity'],40);self.assertTrue(b.order_event.is_set())
        r._broker_order_status.assert_not_called()
    def test_broker_adapters_reject_market_orders(self):
        with patch.object(UpstoxClient,'load_upstox_instrument_map'):
            clients=[BaseBrokerClient(Settings()),UpstoxClient(Settings())]
        for b in clients:
            with self.assertRaisesRegex(ValueError,'LIMIT'):
                b.place_order(exchange='NFO',tradingsymbol='TEST',side='BUY',quantity=1,product_type='I',price_type='MKT',price=100,trigger_price=0,confirm_live=True)
    def test_paper_gap_exit_does_not_fake_fill(self):
        r=runner();r.request.live_trade=False;o=make_order(live_trade=False,stoploss=95)
        r._quote_snapshot.return_value=({'ltp':93,'best_buy':93,'best_sell':93.1},None)
        self.assertFalse(r._close_order(o,'SL',NOW))
        self.assertEqual(o['quantity'],100);self.assertEqual(o['exit_intent']['boundary'],94.5)
    def test_candle_confirmed_after_start_is_prefetched(self):
        r=runner();r.started_at=(NOW+timedelta(minutes=1)).isoformat()
        r._resolve_contract=Mock(return_value={});r._entry_price=Mock(return_value=(100,None))
        signal=dict(time=NOW.isoformat(),side='BUY',option_type='CE',strike=22000)
        r._prefetch_pending_signals({'signals':[signal]},r.request,NOW+timedelta(minutes=5))
        r._resolve_contract.assert_called_once()

    def test_continuous_exit_submission_is_reserved_once(self):
        import threading
        import time
        r=runner();del r._start_execution_manager
        o=make_order(stoploss=95);r.paper_orders=[o]
        submitted=threading.Event();release=threading.Event()
        def place(**kwargs):
            submitted.set();release.wait(2)
            return {"stat":"Ok","norenordno":"id"}
        r.broker.place_order.side_effect=place
        r._close_order(o,'SL',NOW)
        self.assertTrue(submitted.wait(2))
        for _ in range(5):r._close_order(o,'SL',NOW)
        release.set()
        deadline=time.monotonic()+2
        while o.get('_management_active') and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(r.broker.place_order.call_count,1)
        self.assertEqual(o['status'],'Exit_Pending')
    def test_slow_quote_does_not_hold_control_lock(self):
        import threading
        r=runner();r.paper_orders=[make_order()]
        entered=threading.Event();release=threading.Event()
        def quote(order):
            entered.set();release.wait(2)
            return {'ltp':100,'best_buy':99.95,'best_sell':100.05},None
        r._quote_snapshot.side_effect=quote
        thread=threading.Thread(target=r._prepare_watch_quotes,args=(r.request,));thread.start()
        self.assertTrue(entered.wait(2))
        acquired=r.order_lock.acquire(timeout=.2)
        if acquired:r.order_lock.release()
        release.set();thread.join(2)
        self.assertTrue(acquired)
    def test_missing_fill_price_blocks_replacement_and_recovers(self):
        r=runner();o=make_order(quantity=0,requested_quantity=100)
        r._ensure_intent(o,'ENTRY','BUY',100);record=r._register(o,'id','ENTRY','BUY',100,99)
        r._broker_order_status.return_value={'status':'OPEN','fillshares':'40'}
        r._manage_execution(o)
        r.broker.cancel_order.assert_not_called();r.broker.place_order.assert_not_called()
        self.assertTrue(o['fill_reconciliation_required'])
        r._apply_broker_status(o,record,{'status':'OPEN','fillshares':'40','avgprc':'100'})
        self.assertEqual(o['quantity'],40);self.assertNotIn('fill_reconciliation_required',o)
    def test_late_websocket_update_does_not_undo_fill(self):
        r=runner();b=BaseBrokerClient(Settings());r.broker.take_order_update=b.take_order_update
        o=make_order(quantity=0);record=r._register(o,'id','ENTRY','BUY',100,100)
        r._apply_broker_status(o,record,{'status':'OPEN','fillshares':'40','avgprc':'100'})
        b.publish_order_update({'norenordno':'id','status':'OPEN','fillshares':'0'})
        r._consume_order_updates(o)
        self.assertEqual(o['quantity'],40);self.assertFalse(o.get('submission_unknown',False))

    def test_live_start_cannot_bypass_bounded_manager(self):
        r=runner();r.request.continuous_execution=False
        with self.assertRaisesRegex(RuntimeError,'bounded continuous'):r.start(r.request)
