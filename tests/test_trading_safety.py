"""Regression tests. All broker requests are local mocks; no credentials are loaded."""
import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from pydantic import ValidationError

from config.settings import Settings
from data.instruments import InstrumentMaster
from execution.runner import IntradayRunner
from models.schemas import IntradayRunRequest, OrderRequest, Signal, StrategyConfig
from strategy.engine import TradeState, _update_trade, run_strategy
from strategy.indicators import rma, supertrend
from utils.clock import EXCHANGE_TZ, candle_start, market_time
from brokers.upstox.client import UpstoxClient
from brokers.base import BaseBrokerClient

NOW = datetime(2026, 10, 7, 10, tzinfo=EXCHANGE_TZ)


def make_order(**values):
    contract=dict(exchange='NFO',token='1',tradingsymbol='NIFTY_TEST',lot_size=1,tick_size=.05)
    order=dict(order_key='o',status='Active',live_trade=True,side='BUY',quantity=100,option_entry=100.,option_ltp=100.,stoploss=80.,initial_stoploss=80.,target=250.,instrument_type='Option',tradingsymbol='NIFTY_TEST',trade_contract=contract,option_contract=contract,spot_entry=22000.,source_signal='BUY',source_signal_time=NOW.isoformat(),entry_time=NOW.isoformat(),time=NOW.isoformat(),tick_size=.05,realized_pnl=0.,pnl=0.,chasing_active=False)
    order.update(values)
    return order


def make_runner(**kwargs):
    broker=SimpleNamespace(settings=Settings(live_trading_enabled=True),connected=True,
        quote_snapshot=Mock(return_value={'ltp':100.,'best_buy':99.,'best_sell':101.,'tick_size':.05}),
        place_order=Mock(return_value={'stat':'Ok','norenordno':'new'}),
        cancel_order=Mock(return_value={'stat':'Ok'}),get_positions=Mock(return_value=None))
    runner=IntradayRunner(broker=broker,**kwargs)
    # Preserve explicit coverage for the legacy synchronous execution path.
    runner.request=IntradayRunRequest(live_trade=True,continuous_execution=False)
    runner.entries_enabled=True
    runner._broker_order_status=Mock(return_value={'norenordno':'new','status':'CANCELLED','fillshares':'0'})
    return runner


class ExecutionSafetyTests(unittest.TestCase):
    def test_unconfirmed_cancel_never_replaces_and_late_fill_remains_tracked(self):
        r=make_runner();o=make_order(status='Entry_Submitting',quantity=0,requested_quantity=100)
        r._broker_order_status.return_value={'status':'OPEN','fillshares':'0'}
        req=r.request.model_copy(update={'chase_max_retries':2,'chase_sweep_market':True})
        with patch('execution.order_manager.monotonic_time.sleep'):
            r._run_price_chasing_loop(o,'BUY',101.,'ENTRY',req,100,True)
        self.assertEqual(r.broker.place_order.call_count,1)
        self.assertEqual(o['status'],'Entry_Pending')
        r._apply_broker_status(o,o['broker_orders'][0],{'status':'COMPLETE','fillshares':'100','avgprc':'101'})
        self.assertEqual((o['status'],o['quantity']),('Active',100))

    def test_partial_chased_entry_tracks_only_actual_quantity(self):
        r=make_runner();o=make_order(status='Entry_Submitting',quantity=0)
        r._broker_order_status.return_value={'status':'CANCELLED','fillshares':'40','avgprc':'101'}
        req=r.request.model_copy(update={'chase_max_retries':1,'chase_sweep_market':False})
        with patch('execution.order_manager.monotonic_time.sleep'):
            r._run_price_chasing_loop(o,'BUY',101.,'ENTRY',req,100,True)
        self.assertEqual((o['status'],o['quantity']),('Active',40))

    def test_partial_chased_exit_retains_remaining_position(self):
        r=make_runner();o=make_order(status='Exit_Submitting')
        r._broker_order_status.return_value={'status':'CANCELLED','fillshares':'40','avgprc':'101'}
        req=r.request.model_copy(update={'chase_max_retries':1,'chase_sweep_market':False})
        with patch('execution.order_manager.monotonic_time.sleep'):
            r._run_price_chasing_loop(o,'SELL',99.,'EXIT',req,100,False)
        self.assertEqual((o['status'],o['quantity']),('Active',60))
        self.assertEqual(o['realized_pnl'],40.)

    def test_cumulative_partial_fills_are_applied_once(self):
        r=make_runner();o=make_order(status='Exit_Pending')
        record=r._register(o,'new','EXIT','SELL',100,99.)
        for status in ({'status':'OPEN','fillshares':'40','avgprc':'102'},)*2:
            r._apply_broker_status(o,record,status)
        self.assertEqual(o['quantity'],60)
        r._apply_broker_status(o,record,{'status':'COMPLETE','fillshares':'100','avgprc':'103'})
        self.assertEqual((o['status'],o['quantity']),('Closed',0))
        self.assertAlmostEqual(o['pnl'],300.)

    def test_oversized_manual_reduction_never_reaches_broker(self):
        r=make_runner();r.paper_orders=[make_order(quantity=65)]
        with self.assertRaisesRegex(ValueError,'Reduction exceeds'):
            r.manual_trade_action('o','SELL',130,'100')
        r.broker.place_order.assert_not_called()

    def test_concurrent_exit_reserves_once(self):
        r=make_runner();o=make_order()
        entered=threading.Event();release=threading.Event()
        def place(**kwargs):
            entered.set();release.wait(3);return {'norenordno':'new'}
        r.broker.place_order.side_effect=place
        worker=threading.Thread(target=r._close_order,args=(o,'SL',NOW,100))
        worker.start();self.assertTrue(entered.wait(2))
        self.assertTrue(r._close_order(o,'SL',NOW,100))
        release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(r.broker.place_order.call_count,1)
        self.assertEqual(o['status'],'Exit_Pending')

    def test_missing_positions_never_close_or_supply_zero_cap(self):
        r=make_runner();o=make_order();r.paper_orders=[o]
        r._sync_broker_positions(r.request)
        self.assertNotIn('broker_net_qty',o)
        self.assertFalse(r.entries_enabled)
        self.assertTrue(r._close_order(o,'SL',NOW,100))
        self.assertEqual(o['status'],'Exit_Pending')
        self.assertEqual(r.broker.place_order.call_count,1)

    def test_zero_broker_cap_is_not_display_only_close(self):
        r=make_runner();o=make_order(broker_net_qty=0)
        self.assertFalse(r._close_order(o,'SL',NOW,100))
        self.assertEqual(o['status'],'Active')
        r.broker.place_order.assert_not_called()

    def test_stop_preserves_protection(self):
        r=make_runner();r.active=True;r.paper_orders=[make_order()]
        state=r.stop()
        self.assertTrue(state['active'])
        self.assertFalse(state['entries_enabled'])
        self.assertEqual(r.paper_orders[0]['status'],'Active')

    def test_submission_timeout_is_quarantined_without_retry(self):
        r=make_runner();o=make_order(status='Entry_Submitting',quantity=0)
        r.broker.place_order.side_effect=TimeoutError()
        req=r.request.model_copy(update={'chase_max_retries':3})
        r._run_price_chasing_loop(o,'BUY',101.,'ENTRY',req,100,True)
        self.assertEqual(r.broker.place_order.call_count,1)
        self.assertEqual(o['status'],'Recovery_Required')
        self.assertFalse(r.entries_enabled)

    def test_conditional_wait_is_not_a_quote_failure(self):
        r=make_runner();o=make_order(status='Idle',live_trade=False)
        req=IntradayRunRequest(entry_order_mode='Limit_Below',entry_limit_price=90)
        with patch('execution.order_manager.market_now',return_value=NOW):
            for _ in range(20):r._process_entry_order(o,req)
        self.assertEqual(o['status'],'Idle')
        self.assertEqual(o.get('quote_retry_count',0),0)

    def test_aggressive_prices_cross_the_spread_and_round_exactly(self):
        r=make_runner();o=make_order()
        self.assertEqual(r._entry_limit_price(o,r.request)[0],101.)
        self.assertEqual(r._exit_limit_price(o,r.request,'SELL')[0],99.)
        self.assertEqual(r._round_to_tick(100.05,.05,'SELL'),100.05)

    def test_wait_for_opposite_exit_before_new_entry(self):
        r=make_runner();o=make_order();r.paper_orders=[o];r.started_at=NOW-timedelta(hours=1)
        sig=dict(time=(NOW-timedelta(minutes=3)).isoformat(),side='SELL',option_type='PE',strike=22000,entry=22000)
        r._close_order=Mock(side_effect=lambda o,*a:(o.update(status='Exit_Pending') or True))
        r._paper_trade_new_today_signals({'signals':[sig]},r.request,NOW,{})
        self.assertEqual(len(r.paper_orders),1)

    def test_max_trade_count_is_enforced_on_opposite_signal(self):
        r=make_runner();r.paper_orders=[make_order()];r.started_at=NOW-timedelta(hours=1)
        req=r.request.model_copy(update={'max_trades_per_day':1})
        sig=dict(time=(NOW-timedelta(minutes=3)).isoformat(),side='SELL',option_type='PE',strike=22000,entry=22000)
        r._close_order=Mock()
        r._paper_trade_new_today_signals({'signals':[sig]},req,NOW,{})
        r._close_order.assert_not_called()
        self.assertEqual(len(r.paper_orders),1)

    def test_state_recovers_open_position_and_pauses_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';r=make_runner(state_path=path)
            r.paper_orders=[make_order()];r.broker_identity=r._identity(r.broker);r._persist_state()
            recovered=IntradayRunner(state_path=path)
            self.assertTrue(recovered.recovery_required)
            self.assertFalse(recovered.entries_enabled)
            self.assertEqual(recovered.paper_orders[0]['quantity'],100)
            self.assertEqual(path.stat().st_mode & 0o077,0)

    def test_status_snapshot_does_not_expose_mutable_state(self):
        r=make_runner();r.paper_orders=[make_order()]
        snapshot=r.status();snapshot['paper_orders'][0]['quantity']=999
        self.assertEqual(r.paper_orders[0]['quantity'],100)

    def test_fill_changes_recompute_risk_without_loosening_stop(self):
        r=make_runner();o=make_order(stoploss=100.)
        r._apply_manual_trade_fill(o,'BUY',100,110)
        self.assertEqual(o['option_entry'],105.)
        self.assertGreaterEqual(o['stoploss'],100.)

    def test_spot_levels_use_underlying_price_and_direction(self):
        r=make_runner();o=make_order(side='SELL',source_signal='BUY',spot_entry=22000.)
        r._reset_risk_levels(o,IntradayRunRequest(sltp_instrument='spot',option_strategy='SELL'))
        self.assertEqual(o['risk_entry'],22000.)
        self.assertEqual(o['risk_direction'],1)
        # Existing option level is replaced when selecting the original risk basis.
        self.assertEqual(o['initial_stoploss'],21980.)

    def test_unknown_status_text_is_not_a_fill(self):
        r=make_runner()
        self.assertEqual(r._mapped_broker_state({'stat':'Not_Ok','emsg':'order not filled'}),'unknown')
        self.assertEqual(r._mapped_broker_state({'status':'CANCELLED','fillshares':'40'}),'cancelled')


class StrategyTests(unittest.TestCase):
    def bars(self,n):
        return [dict(time=NOW+timedelta(minutes=i),open=100+i,high=102+i,low=99+i,close=101+i,volume=100) for i in range(n)]

    def test_supertrend_initializes_after_atr_warmup(self):
        result=supertrend(pd.DataFrame(self.bars(100)),10,3)
        self.assertEqual(result.supertrend.notna().sum(),91)

    def test_rma_seeds_with_sma(self):
        actual=rma(pd.Series([1.,2.,3.,4.]),3)
        self.assertTrue(pd.isna(actual.iloc[1]))
        self.assertEqual(actual.iloc[2],2.)
        self.assertAlmostEqual(actual.iloc[3],8/3)

    def test_short_history_is_not_fabricated(self):
        result=run_strategy(self.bars(10))
        self.assertEqual(result['summary']['bars'],10)
        self.assertFalse(result['summary']['warmup_complete'])
        self.assertEqual(result['signals'],[])
        self.assertEqual(len(result['bars']),10)

    def test_stop_precedes_new_trailing_level_on_ambiguous_bar(self):
        sig=Signal(time=NOW,side='BUY',option_type='CE',strike=100,entry=100,stop_loss=90,tp1=110,tp2=120,tp3=130,tsl=90,status='OPEN')
        state=TradeState(sig)
        _update_trade(state,pd.Series(dict(time=NOW,open=100,low=89,high=111,close=100)),StrategyConfig(),False)
        self.assertEqual((sig.status,sig.pnl,sig.tsl),('SL',-5.,90.))
        self.assertTrue(sig.closed)

    def test_tp1_not_terminal_but_composite_terminal_exit_is_honoured(self):
        r=make_runner();o=make_order();req=IntradayRunRequest(sltp_instrument='strategy')
        def strategy(status):return {'signals':[dict(time=o['source_signal_time'],side='BUY',status=status)]}
        self.assertIsNone(r._sync_strategy_sltp(o,req,strategy('TP1')))
        self.assertEqual(r._sync_strategy_sltp(o,req,strategy('TP1_TSL')),'STRATEGY_TP1_TSL')

    def test_bad_config_is_rejected_server_side(self):
        for data in ({'adx_length':0},{'ss_target':-20}):
            with self.assertRaises(ValidationError):StrategyConfig(**data)
        for data in ({'max_loss':1},{'stoploss_value':-1},{'start_time':'oops'},{'live_trade':True,'exit_order_mode':'False'}):
            with self.assertRaises(ValidationError):IntradayRunRequest(**data)
        with self.assertRaises(ValidationError):OrderRequest(tradingsymbol='X',side='BUY',quantity=-1)

    def test_invalid_ohlc_rejected(self):
        bars=self.bars(10);bars[0]['high']=10
        with self.assertRaisesRegex(ValueError,'OHLC'):run_strategy(bars)


class AdapterTests(unittest.TestCase):
    def test_explicit_future_expiry_respected_and_expired_contracts_excluded(self):
        im=InstrumentMaster(Path('/tmp'),{})
        df=pd.DataFrame([dict(Symbol='NIFTY',Instrument='FUTIDX',TradingSymbol=s,Expiry=e,Token=i,LotSize=1) for i,(s,e) in enumerate([('EXPIRED','01-01-2020'),('OCT','29-10-2026'),('NOV','26-11-2026')],1)])
        im.frames['NFO']=im._normalize(df,'NFO')
        with patch('data.instruments.market_now',return_value=NOW):
            self.assertEqual(im.resolve_future('NIFTY','NFO','26-11-2026')['tradingsymbol'],'NOV')
            self.assertEqual(im.resolve_future('NIFTY','NFO')['tradingsymbol'],'OCT')

    def test_upstox_products_correct_and_live_calls_mocked(self):
        client=object.__new__(UpstoxClient);client.settings=Settings(live_trading_enabled=True);client._session_token='TEST_ONLY';client.shoonya_to_upstox_key=Mock(return_value='NSE_FO|TEST')
        response=Mock(status_code=200);response.json.return_value={'status':'success','data':{'order_id':'test'}}
        for product,expected in [('I','I'),('M','D')]:
            with patch('brokers.upstox.client.requests.post',return_value=response) as post:
                client.place_order(exchange='NFO',tradingsymbol='TEST',side='BUY',quantity=1,product_type=product,price_type='LMT',price=100,confirm_live=True)
                self.assertEqual(post.call_args.kwargs['json']['product'],expected)

    def test_previous_close_is_not_ltp(self):
        self.assertIsNone(BaseBrokerClient.quote_ltp({'c':'100'}))
        self.assertIsNone(BaseBrokerClient.quote_ltp({'lp':'NaN'}))

    def test_timezone_conversion_and_session_anchored_candle(self):
        utc=datetime(2026,10,7,4,tzinfo=timezone.utc)
        self.assertEqual(market_time(utc).hour,9)
        self.assertEqual(candle_start(market_time(utc),60).strftime('%H:%M'),'09:15')
        self.assertEqual(candle_start(NOW,7).strftime('%H:%M'),'09:57')

    def test_aware_signal_filter_is_comparable(self):
        r=make_runner();r.started_at=NOW;r.seen_signal_keys=set()
        r._resolve_contract=Mock();r._entry_price=Mock(return_value=(100,None))
        sig=dict(time=(NOW-timedelta(minutes=1)).isoformat(),side='BUY',option_type='CE',strike=22000)
        self.assertEqual(r._prefetch_pending_signals({'signals':[sig]},r.request,NOW),{})


if __name__=='__main__':
    unittest.main()
