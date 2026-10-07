"""Authentication, OAuth, durable state and broker-adapter regression checks."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import timedelta

from config.settings import Settings, save_token_to_env, get_settings
from brokers.base import BaseBrokerClient
from brokers.upstox.client import UpstoxClient
from utils.security import authorized, acquire_engine_lock, control_token
from test_trading_safety import NOW, make_runner, make_order
from models.schemas import OrderAdjustRequest


def request(host='127.0.0.1',peer='127.0.0.1',headers=None,cookies=None):
    return SimpleNamespace(client=SimpleNamespace(host=peer),url=SimpleNamespace(hostname=host,scheme='http',netloc=host+':8005'),headers=headers or {},cookies=cookies or {})

class SecurityTests(unittest.TestCase):
    def test_local_api_requires_token(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'ALGO_RUNTIME_DIR':folder},clear=True):
            self.assertFalse(authorized(request()))
            token=control_token()
            self.assertTrue(authorized(request(cookies={'algo_session':token})))
            self.assertEqual(Path(folder,'control_api_token').stat().st_mode & 0o777,0o600)
    def test_cross_origin_rejected_even_with_valid_token(self):
        with patch.dict(os.environ,{'CONTROL_API_TOKEN':'x'*32},clear=True):
            self.assertFalse(authorized(request(headers={'authorization':'Bearer '+'x'*32,'origin':'http://evil.example'})))
    def test_remote_access_requires_explicit_token(self):
        with patch.dict(os.environ,{'CONTROL_API_TOKEN':'x'*32},clear=True):
            self.assertTrue(authorized(request('algo.example','192.0.2.1',{'authorization':'Bearer '+'x'*32})))
            self.assertFalse(authorized(request('evil.example','127.0.0.1')))
    def test_short_control_token_rejected(self):
        with patch.dict(os.environ,{'CONTROL_API_TOKEN':'short'},clear=True), self.assertRaises(RuntimeError):
            control_token()
    def test_single_process_lock(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'ALGO_RUNTIME_DIR':folder}):
            handle=acquire_engine_lock()
            try:
                with self.assertRaises(RuntimeError): acquire_engine_lock()
            finally: handle.close()
            acquire_engine_lock().close()
    def test_oauth_state_invalid_expired_and_replay_rejected(self):
        b=BaseBrokerClient(Settings());state=b._begin_oauth()
        self.assertFalse(b._consume_oauth_state('wrong'))
        self.assertTrue(b._consume_oauth_state(state))
        self.assertFalse(b._consume_oauth_state(state))
        state=b._begin_oauth();b._oauth_deadline=0
        self.assertFalse(b._consume_oauth_state(state))
    def test_upstox_manual_login_does_not_require_missing_totp_package(self):
        with patch.object(UpstoxClient,'load_upstox_instrument_map'):
            b=UpstoxClient(Settings(upstox_api_key='test',upstox_api_secret='test'))
        self.assertFalse(b.connect_auto_oauth())
        self.assertIn('AUTH_REQUIRED:',b.last_error)
        self.assertIn('state=',b.last_error)
        self.assertFalse(b.connect_oauth_code('code','wrong'))
    def test_runtime_tokens_do_not_rewrite_dotenv(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'ALGO_RUNTIME_DIR':folder}):
            save_token_to_env('test-token','zebu')
            self.assertEqual(Path(folder,'session_tokens.json').stat().st_mode & 0o777,0o600)
            self.assertIn('test-token',Path(folder,'session_tokens.json').read_text())
        get_settings.cache_clear()
    def test_upstox_current_session_combines_intraday_and_history(self):
        with patch.object(UpstoxClient,'load_upstox_instrument_map'):
            b=UpstoxClient(Settings())
        b._session_token='test-token'
        response=Mock(status_code=200);response.json.return_value={'data':{'candles':[]}}
        with patch('brokers.upstox.client.market_now',return_value=NOW),patch('brokers.upstox.client.requests.get',return_value=response) as get:
            b.get_bars('NSE','26000',1,NOW-timedelta(days=2),NOW)
        self.assertEqual(get.call_count,2)
        self.assertIn('/intraday/',get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs['headers']['Authorization'],'Bearer test-token')
    def test_spot_move_to_cost_uses_underlying_basis(self):
        r=make_runner();o=make_order(risk_direction=-1,risk_entry=22000,stoploss=22050)
        r.paper_orders=[o]
        r.adjust_order(OrderAdjustRequest(order_key='o',move_sl_to_cost=True))
        self.assertEqual(o['stoploss'],22000)
        r.adjust_order(OrderAdjustRequest(order_key='o',move_sl_to_cost=False))
        self.assertFalse(o['move_sl_to_cost'])
    def test_manual_missing_order_and_derivative_lot_validation(self):
        r=make_runner()
        with self.assertRaises(RuntimeError):r.manual_trade_action('missing','SELL',1,'100')
        o=make_order();o['trade_contract']['lot_size']=65
        with self.assertRaises(ValueError):r._validate_order_size(o,100)
    def test_definitive_rejection_clears_submission_intent(self):
        r=make_runner();o=make_order(quantity=0,status='Entry_Submitting')
        r.broker.place_order.return_value={'stat':'Not_Ok','emsg':'Rejected'}
        r._submit(o,'BUY',100,'ENTRY',r.request,100)
        self.assertNotIn('submission_attempt',o)
        self.assertEqual(o['status'],'Entry_Rejected')

    def test_sdk_timeout_is_local_and_bounded(self):
        from utils.http_transport import BoundedRequests, bound_sdk_http
        import requests
        proxy=BoundedRequests(requests)
        with patch.object(requests,'post',return_value={}) as post:
            proxy.post('https://example.invalid')
            self.assertEqual(post.call_args.kwargs['timeout'],(3.05,10))
        from myntapi import app_oauth
        api=app_oauth()
        original=api.get_quotes.__func__
        bound_sdk_http(api)
        self.assertIs(original.__globals__['requests'],requests)
        self.assertIsInstance(api.get_quotes.__func__.__globals__['requests'],BoundedRequests)

    def test_order_registration_is_idempotent_during_recovery(self):
        r=make_runner();o=make_order(quantity=0)
        first=r._register(o,'id','ENTRY','BUY',100,100)
        self.assertIs(first,r._register(o,'id','ENTRY','BUY',100,100))
        self.assertEqual(len(o['broker_orders']),1)
    def test_corrupt_state_is_not_overwritten(self):
        from execution.runner import IntradayRunner
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder,'state.json');path.write_text('broken')
            r=IntradayRunner(None,state_path=path)
            self.assertTrue(r.recovery_required)
            with self.assertRaises(RuntimeError):r._persist_state()
            self.assertEqual(path.read_text(),'broken')

    def test_dashboard_cross_site_top_level_navigation_allowed(self):
        from utils.security import dashboard_access_allowed
        r=request(headers={'sec-fetch-site':'cross-site','sec-fetch-mode':'navigate','sec-fetch-dest':'document'})
        r.method='GET'
        self.assertTrue(dashboard_access_allowed(r))
        with patch.dict(os.environ,{'CONTROL_API_TOKEN':'x'*32},clear=True):
            r.cookies={'algo_session':'x'*32}
            self.assertFalse(authorized(r))
    def test_dashboard_cross_site_fetch_frame_and_post_rejected(self):
        from utils.security import dashboard_access_allowed
        for mode,dest,method in [('cors','empty','GET'),('navigate','iframe','GET'),('navigate','document','POST')]:
            r=request(headers={'sec-fetch-site':'cross-site','sec-fetch-mode':mode,'sec-fetch-dest':dest})
            r.method=method
            self.assertFalse(dashboard_access_allowed(r))
    def test_dashboard_explicit_foreign_origin_rejected(self):
        from utils.security import dashboard_access_allowed
        r=request(headers={'origin':'http://evil.example','sec-fetch-mode':'navigate','sec-fetch-dest':'document'})
        r.method='GET'
        self.assertFalse(dashboard_access_allowed(r))
