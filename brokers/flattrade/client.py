from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from queue import Queue
import requests
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from config.settings import Settings, save_token_to_env
from brokers.base import BaseBrokerClient
from utils.http_transport import bound_sdk_http
try:
    from brokers.flattrade.api import FlatTradeApiPy
except ImportError:
    try:
        from NorenRestApiPy.NorenApi import NorenApi as FlatTradeApiPy
    except ImportError:
        FlatTradeApiPy = None


def patch_noren_websocket_backoff():
    try:
        import time
        import threading
        import logging
        try:
            from NorenRestApiPy.NorenApi import NorenApi
            from NorenRestApiPy.NorenApi_oauth import NorenApi_oauth
        except ImportError:
            from myntapi.noren import NorenApi
            from myntapi.noren_oauth import NorenApi_oauth

        logger = logging.getLogger(__name__)

        def make_patched_ws_run_forever(class_name, stop_event_attr, websocket_attr):
            def patched_ws_run_forever(self):
                backoff = 2.0
                print(f"[WS PATCH] Patched WebSocket run loop active for {class_name}", flush=True)
                while getattr(self, stop_event_attr).is_set() == False:
                    try:
                        start_time = time.time()
                        ws = getattr(self, websocket_attr)
                        if ws:
                            ws.run_forever(ping_interval=3, ping_payload='{"t":"h"}')
                        duration = time.time() - start_time
                        if duration > 10.0:
                            backoff = 2.0
                    except Exception as e:
                        logger.warning(f"websocket run forever ended in exception, {e}")
                    sleep_until = time.time() + backoff
                    while time.time() < sleep_until and getattr(self, stop_event_attr).is_set() == False:
                        time.sleep(0.5)
                    backoff = min(backoff * 2.0, 60.0)
            return patched_ws_run_forever

        def make_patched_close_websocket(class_name, stop_event_attr, websocket_attr, ws_thread_attr, connected_attr):
            def patched_close_websocket(self):
                print(f"[WS PATCH] Patched close_websocket called for {class_name}", flush=True)
                stop_event = getattr(self, stop_event_attr, None)
                if stop_event:
                    stop_event.set()
                setattr(self, connected_attr, False)
                ws = getattr(self, websocket_attr, None)
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                ws_thread = getattr(self, ws_thread_attr, None)
                if ws_thread and ws_thread.is_alive():
                    if threading.current_thread() != ws_thread:
                        try:
                            ws_thread.join(timeout=2.0)
                        except Exception:
                            pass
            return patched_close_websocket

        if hasattr(NorenApi, '_NorenApi__ws_run_forever'):
            NorenApi._NorenApi__ws_run_forever = make_patched_ws_run_forever(
                "NorenApi", "_NorenApi__stop_event", "_NorenApi__websocket"
            )
            NorenApi.close_websocket = make_patched_close_websocket(
                "NorenApi", "_NorenApi__stop_event", "_NorenApi__websocket", "_NorenApi__ws_thread", "_NorenApi__websocket_connected"
            )
        if NorenApi_oauth and hasattr(NorenApi_oauth, '_NorenApi_oauth__ws_run_forever'):
            NorenApi_oauth._NorenApi_oauth__ws_run_forever = make_patched_ws_run_forever(
                "NorenApi_oauth", "_NorenApi_oauth__stop_event", "_NorenApi_oauth__websocket"
            )
            NorenApi_oauth.close_websocket = make_patched_close_websocket(
                "NorenApi_oauth", "_NorenApi_oauth__stop_event", "_NorenApi_oauth__websocket", "_NorenApi_oauth__ws_thread", "_NorenApi_oauth__websocket_connected"
            )
        print("[WS PATCH] Monkey patched NorenApi WebSocket run loop and close_websocket with backoff & leak protection.", flush=True)
    except Exception as e:
        print(f"[WS PATCH] Failed to apply monkey patch: {e}", flush=True)

patch_noren_websocket_backoff()


class FlatTradeClient(BaseBrokerClient):
    def __init__(self, settings: Settings):
        super().__init__(settings)

    def _validate_token(self) -> bool:
        if not self.settings.flattrade_access_token or self.settings.flattrade_access_token == "placeholder":
            return False
        return super()._validate_token()

    def connect_with_token(self) -> bool:
        if self.connected:
            return True
        if not self.settings.flattrade_access_token:
            self.last_error = "No FLATTRADE_ACCESS_TOKEN found. OAuth browser login is required."
            return False
        try:
            if FlatTradeApiPy is None:
                self.last_error = "FlatTradeApiPy is not installed or importable."
                return False
            self.api = bound_sdk_http(FlatTradeApiPy())
            token = self.settings.flattrade_access_token
            suser_token = self.settings.flattrade_suser_token or token
            if token and hasattr(self.api, "set_token"):
                self.api.set_token(token)
            elif token and hasattr(self.api, "set_session"):
                self.api.set_session(userid=self.settings.flattrade_user_id, password="", usertoken=suser_token)
            if not self._validate_token():
                self.api = None
                self.last_error = "FLATTRADE_ACCESS_TOKEN is expired or invalid. Will attempt OAuth re-login."
                print("[AUTH] Stored FlatTrade token is expired — falling through to OAuth re-login.", flush=True)
                return False
            self.connected = True
            self._session_token = token
            self._quote_cache = {}
            self._quote_cache_time = {}
            self._token_cache = {}
            self.ensure_instruments_daily()
            self.start_websocket()
            print("[AUTH] FlatTrade Token login successful — session validated.", flush=True)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.connected = False
            return False

    def connect_auto_oauth(self, timeout_seconds: int = 180) -> bool:
        if self.connected:
            return True
        if not self.settings.flattrade_api_key or not self.settings.flattrade_api_secret:
            self.last_error = "FlatTrade API Key/API Secret missing in credentials"
            return False
        if not self.settings.flattrade_user_id or not self.settings.flattrade_password:
            self.last_error = "FlatTrade User ID/password missing in credentials"
            return False
        oauth_state = self._begin_oauth()
        callback_state = ""
        code_queue: Queue[str] = Queue()
        server = self._start_callback_server(code_queue)
        driver = None
        try:
            from selenium import webdriver  # type: ignore
            from selenium.webdriver.common.by import By  # type: ignore
            from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
            options = webdriver.ChromeOptions()
            options.add_argument("--window-size=1440,960")
            options.add_argument("--headless=new")
            options.add_argument("--disable-dev-shm-usage")
            driver = webdriver.Chrome(options=options)
            wait = WebDriverWait(driver, 20)
            from selenium.common.exceptions import StaleElementReferenceException  # type: ignore
            auth_url = f"https://auth.flattrade.in/?app_key={self.settings.flattrade_api_key}"
            auth_url += "&state=" + oauth_state
            driver.get(auth_url)
            filled_and_submitted = False
            for attempt in range(5):
                try:
                    inputs = wait.until(lambda d: [x for x in d.find_elements(By.TAG_NAME, "input") if x.is_displayed()])
                    text_inputs = [x for x in inputs if (x.get_attribute("type") or "text").lower() not in {"hidden", "submit", "button"}]
                    if not text_inputs:
                        time.sleep(0.5)
                        continue
                    self._fill_input(text_inputs[0], self.settings.flattrade_user_id)
                    if len(text_inputs) > 1:
                        self._fill_input(text_inputs[1], self.settings.flattrade_password)
                    otp = self._totp()
                    if otp and len(text_inputs) > 2:
                        self._fill_input(text_inputs[2], otp)
                    buttons = [b for b in driver.find_elements(By.TAG_NAME, "button") if b.is_displayed()]
                    if buttons:
                        try:
                            buttons[0].click()
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click();", buttons[0])
                            except Exception as click_err:
                                print(f"Selenium button click failed: {click_err}", flush=True)
                    filled_and_submitted = True
                    break
                except StaleElementReferenceException:
                    time.sleep(0.5)
                    continue
            if not filled_and_submitted:
                self.last_error = "Failed to fill and submit credentials due to stale element reference. Please retry."
                return False
            auth_code = None
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                auth_code = self._extract_auth_code(driver.current_url)
                if auth_code:
                    callback_state = parse_qs(urlparse(driver.current_url).query).get("state", [""])[0]
                    break
                if not code_queue.empty():
                    auth_code, callback_state = code_queue.get_nowait()
                    break
                time.sleep(1)
            if not auth_code:
                self.last_error = "Timed out waiting for FlatTrade OAuth code."
                return False
            return self.connect_oauth_code(auth_code, state=callback_state)
        except Exception as exc:
            self.last_error = str(exc)
            return False
        finally:
            if server:
                server.shutdown()
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    def connect_oauth_code(self, auth_code: str, state: str = "") -> bool:
        if not self._consume_oauth_state(state):
            return False
        try:
            api_key = self.settings.flattrade_api_key
            api_secret = self.settings.flattrade_api_secret
            raw_concat = api_key + auth_code + api_secret
            api_secret_sha256 = hashlib.sha256(raw_concat.encode("utf-8")).hexdigest()
            
            payload = {
                "api_key": api_key,
                "request_code": auth_code,
                "api_secret": api_secret_sha256,
            }
            resp = requests.post("https://authapi.flattrade.in/trade/apitoken", json=payload, timeout=15)
            response = resp.json()
            if not response or response.get("stat") == "Not_Ok" or response.get("status") == "Error":
                self.last_error = response.get("emsg") or response.get("message") or "FlatTrade OAuth token exchange failed"
                return False
            
            token = (
                response.get("token")
                or response.get("access_token")
                or response.get("usertoken")
                or ""
            )
            suser_token = response.get("suserToken") or response.get("susertoken") or token

            if not token:
                self.last_error = "No access token in FlatTrade OAuth response"
                return False

            if FlatTradeApiPy is None:
                self.last_error = "FlatTradeApiPy is not installed or importable."
                return False

            self.api = bound_sdk_http(FlatTradeApiPy())
            if hasattr(self.api, "set_token"):
                self.api.set_token(token)
            elif hasattr(self.api, "set_session"):
                self.api.set_session(
                    userid=self.settings.flattrade_user_id,
                    password="",
                    usertoken=suser_token,
                )

            self.connected = True
            self._session_token = token
            self._quote_cache = {}
            self._quote_cache_time = {}
            self._token_cache = {}
            self.last_error = None

            try:
                save_token_to_env(token=token, broker="flattrade", suser_token=suser_token)
                print("[AUTH] FlatTrade Access token saved to .env successfully.", flush=True)
            except Exception as save_exc:
                print(f"[AUTH] Warning: could not save FlatTrade token to .env: {save_exc}", flush=True)

            self.ensure_instruments_daily()
            self.start_websocket()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def status(self) -> dict:
        return {
            "broker": "flattrade",
            "connected": self.connected,
            "live_trading_enabled": self.settings.live_trading_enabled,
            "creds_txt_loaded": self.settings.creds_txt_loaded,
            "has_user_id": bool(self.settings.flattrade_user_id),
            "has_api_key": bool(self.settings.flattrade_api_key),
            "has_api_secret": bool(self.settings.flattrade_api_secret),
            "has_password": bool(self.settings.flattrade_password),
            "has_totp": bool(self.settings.flattrade_totp_secret),
            "has_redirect_url": bool(self.settings.flattrade_redirect_url),
            "has_access_token": bool(self.settings.flattrade_access_token),
            "last_error": self.last_error,
            "instrument_exchanges": sorted(self.instruments.frames.keys()),
        }

    def _totp(self) -> str | None:
        if not self.settings.flattrade_totp_secret:
            return None
        try:
            import pyotp  # type: ignore
            return pyotp.TOTP(self.settings.flattrade_totp_secret).now()
        except Exception:
            return None

    @staticmethod
    def _fill_input(element: Any, value: str) -> None:
        element.clear()
        element.send_keys(value)

    @staticmethod
    def _extract_auth_code(url: str) -> str | None:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        code = params.get("code") or params.get("request_code")
        return code[0] if code else None

    def _start_callback_server(self, code_queue: Queue[str]) -> HTTPServer | None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                code = FlatTradeClient._extract_auth_code(self.path)
                if code:
                    code_queue.put((code, parse_qs(urlparse(self.path).query).get("state", [""])[0]))
                    body = b"<h2>FlatTrade login successful. You can close this tab.</h2>"
                else:
                    body = b"<h2>FlatTrade callback received without code.</h2>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_: Any) -> None:
                return
        ports = [8189, 8190, 8080]
        if self.settings.flattrade_redirect_url:
            parsed = urlparse(self.settings.flattrade_redirect_url)
            if parsed.port:
                ports.insert(0, parsed.port)
        for port in dict.fromkeys(ports):
            try:
                server = HTTPServer(("127.0.0.1", port), Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                return server
            except OSError:
                continue
        return None

    def _ws_callback_socket_open(self) -> None:
        print("[WS] FlatTrade WebSocket connection established successfully", flush=True)
        self._ws_feed_opened = True
        if self._ws_subscribed and self.api:
            try:
                self.api.subscribe(list(self._ws_subscribed))
                print(f"[WS] FlatTrade Re-subscribed to {len(self._ws_subscribed)} symbols", flush=True)
            except Exception as e:
                print(f"[WS] FlatTrade Re-subscription error: {e}", flush=True)

    def _ws_callback_socket_closed(self) -> None:
        print("[WS] FlatTrade WebSocket connection closed", flush=True)
        self._ws_feed_opened = False

    def _ws_callback_socket_error(self, err: Any) -> None:
        print(f"[WS] FlatTrade WebSocket Error: {err}", flush=True)

    def _ws_callback_order_update(self, tick_data: dict[str, Any]) -> None:
        self.publish_order_update(tick_data)
        order_id = tick_data.get("norenordno", "Unknown")
        status = tick_data.get("status", "Unknown")
        symbol = tick_data.get("tsym", "Unknown")
        avg_price = tick_data.get("avgprc", "0")
        print(f"[WS] FlatTrade Order update - ID: {order_id}, Status: {status}, Symbol: {symbol}, Avg Price: {avg_price}", flush=True)

    def _ws_callback_quote_update(self, inmessage: dict[str, Any]) -> None:
        try:
            exchange = inmessage.get("e")
            token = inmessage.get("tk")
            if not exchange or not token:
                return
            key = (exchange.upper(), str(token))
            if key not in self._ws_quotes:
                self._ws_quotes[key] = {}
            if "lp" in inmessage:
                self._ws_quote_time[key] = time.monotonic()
            fields = ["lp", "pc", "o", "h", "l", "c", "bp1", "sp1", "v", "oi"]
            for field in fields:
                if field in inmessage:
                    try:
                        self._ws_quotes[key][field] = float(inmessage[field])
                    except (ValueError, TypeError):
                        self._ws_quotes[key][field] = inmessage[field]
            if "lp" in self._ws_quotes[key]:
                self._ws_quotes[key]["ltp"] = self._ws_quotes[key]["lp"]
            if "pc" in self._ws_quotes[key]:
                self._ws_quotes[key]["previous_close"] = self._ws_quotes[key]["pc"]
        except Exception:
            pass

    def start_websocket(self) -> None:
        self.ensure_connected()
        if not self.connected or not self.api:
            return
        if self._ws_feed_opened:
            return
        def run_ws():
            try:
                print("[WS] Starting FlatTrade WebSocket thread...", flush=True)
                self.api.start_websocket(
                    order_update_callback=self._ws_callback_order_update,
                    subscribe_callback=self._ws_callback_quote_update,
                    socket_open_callback=self._ws_callback_socket_open,
                    socket_close_callback=self._ws_callback_socket_closed,
                    socket_error_callback=self._ws_callback_socket_error,
                )
                ws_app = getattr(self.api, '_NorenApi__websocket', None)
                if ws_app is not None:
                    import json as _pj
                    def _patched_on_data(ws, message, data_type, continue_flag):
                        try:
                            res = _pj.loads(message)
                        except Exception:
                            return
                        if res.get('t') in ('tk','tf','dk','df'):
                            self._ws_callback_quote_update(res)
                            return
                        if res.get('t') == 'om':
                            self._ws_callback_order_update(res)
                            return
                        if res.get('t') in ('ck','ak'):
                            if res.get('s') == 'OK':
                                print("[WS] FlatTrade WS Auth OK - feed_opened!", flush=True)
                                self._ws_callback_socket_open()
                            else:
                                print(f"[WS] FlatTrade WS Auth REJECTED: {res}", flush=True)
                    ws_app.on_data = _patched_on_data
                    print("[WS] FlatTrade WS Patch applied successfully", flush=True)
                for _ in range(10):
                    if self._ws_feed_opened:
                        break
                    time.sleep(1)
                if self._ws_feed_opened:
                    try:
                        self.api.subscribe_orders()
                        print("[WS] FlatTrade Subscribed to orders successfully", flush=True)
                    except Exception as sub_e:
                        print(f"[WS] FlatTrade Order subscription warning: {sub_e}", flush=True)
                else:
                    print("[WS] FlatTrade WebSocket feed failed to open within timeout", flush=True)
            except Exception as e:
                print(f"[WS] FlatTrade WebSocket runner error: {e}", flush=True)
        threading.Thread(target=run_ws, daemon=True).start()
