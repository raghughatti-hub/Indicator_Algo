from utils.clock import market_now, market_time, candle_start
from datetime import datetime, date, timedelta
import gzip
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from queue import Queue
import re
import shutil
import threading
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import pandas as pd
import requests

from config.settings import Settings, save_token_to_env, BASE_DIR
from brokers.base import BaseBrokerClient

logger = logging.getLogger("AlgoTrading.UpstoxClient")


class UpstoxClient(BaseBrokerClient):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.api_client: Any = None
        self.upstox_key_map: dict[str, str] = {}
        self.upstox_tsym_map: dict[str, str] = {}
        self.upstox_token_map: dict[str, str] = {}
        self.upstox_token_to_key: dict[str, str] = {}
        
        # Websocket V3 Streamer state
        self.v3_streamer: Any = None
        self.subscribed_v3_keys: set[str] = set()
        self.v3_key_to_req: dict[str, str] = {}
        
        # Load instrument mappings
        self.load_upstox_instrument_map()

    def _validate_token(self) -> bool:
        if not self._session_token or self._session_token == "placeholder":
            return False
        # Validate by calling profile endpoint
        url = "https://api.upstox.com/v2/user/profile"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session_token}"
        }
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                self.verified_account_id = str(r.json().get("data", {}).get("user_id", ""))
                return bool(self.verified_account_id)
            return False
        except Exception:
            return False

    def connect_with_token(self) -> bool:
        if self.connected:
            return True
        if not self.settings.upstox_access_token:
            self.last_error = "No UPSTOX_ACCESS_TOKEN found. Browser login is required."
            return False

        try:
            self._session_token = self.settings.upstox_access_token
            if not self._validate_token():
                self._session_token = None
                self.last_error = "UPSTOX_ACCESS_TOKEN is expired or invalid."
                print("[AUTH] Stored Upstox token is expired — falling through to OAuth login.", flush=True)
                return False

            self.connected = True
            self._quote_cache = {}
            self._quote_cache_time = {}
            self._token_cache = {}
            self.ensure_instruments_daily()
            self.start_websocket()
            print("[AUTH] Upstox token login successful — session validated.", flush=True)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.connected = False
            return False

    def connect_auto_oauth(self, timeout_seconds: int = 180) -> bool:
        if self.connected:
            return True
        if not self.settings.upstox_api_key or not self.settings.upstox_api_secret:
            self.last_error = "Upstox API key and secret are required"
            return False
        params = {"response_type": "code", "client_id": self.settings.upstox_api_key,
                  "redirect_uri": self.settings.upstox_redirect_url or "http://localhost:8005/upstox/callback",
                  "state": self._begin_oauth()}
        self.last_error = "AUTH_REQUIRED:https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params)
        return False

    def connect_oauth_code(self, auth_code: str, state: str = "") -> bool:
        if not self._consume_oauth_state(state):
            return False
        try:
            url = "https://api.upstox.com/v2/login/authorization/token"
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            redirect_url = self.settings.upstox_redirect_url or "http://localhost:8005/upstox/callback"
            payload = {
                "code": auth_code,
                "client_id": self.settings.upstox_api_key,
                "client_secret": self.settings.upstox_api_secret,
                "redirect_uri": redirect_url,
                "grant_type": "authorization_code"
            }

            r = requests.post(url, data=payload, headers=headers, timeout=15)
            if r.status_code != 200:
                self.last_error = f"Upstox token API returned HTTP {r.status_code}"
                return False

            res_data = r.json()
            access_token = res_data.get("access_token")
            if not access_token:
                self.last_error = "No access_token found in Upstox response"
                return False

            self._session_token = access_token
            if not self._validate_token():
                self.last_error = "Cannot verify authenticated Upstox account"
                return False
            self.connected = True
            self._quote_cache = {}
            self._quote_cache_time = {}
            self._token_cache = {}
            self.last_error = None

            # Persist Token to .env
            try:
                save_token_to_env(access_token, "upstox")
                print(f"[AUTH] Upstox Access token saved to .env successfully.", flush=True)
            except Exception as env_exc:
                print(f"[AUTH] Warning: could not save Upstox token: {env_exc}", flush=True)

            self.ensure_instruments_daily()
            self.start_websocket()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def status(self) -> dict:
        return {
            "broker": "upstox",
            "connected": self.connected,
            "live_trading_enabled": self.settings.live_trading_enabled,
            "creds_txt_loaded": self.settings.creds_txt_loaded,
            "has_user_id": bool(self.settings.upstox_user_id),
            "has_api_key": bool(self.settings.upstox_api_key),
            "has_api_secret": bool(self.settings.upstox_api_secret),
            "has_password": bool(self.settings.upstox_password),
            "has_totp": bool(self.settings.upstox_totp_secret),
            "has_redirect_url": bool(self.settings.upstox_redirect_url),
            "has_access_token": bool(self.settings.upstox_access_token),
            "last_error": self.last_error,
            "instrument_exchanges": sorted(self.instruments.frames.keys()) + (["UPSTOX"] if self.upstox_key_map else []),
        }

    def _totp(self) -> str | None:
        if not self.settings.upstox_totp_secret:
            return None
        try:
            import pyotp
            return pyotp.TOTP(self.settings.upstox_totp_secret.strip()).now()
        except Exception:
            return None

    @staticmethod
    def _fill_input_robust(driver: Any, element: Any, value: str) -> None:
        try:
            element.click()
            element.clear()
            element.send_keys(value)
        except Exception:
            pass
        try:
            driver.execute_script("""
                arguments[0].value = arguments[1];
                arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
            """, element, value)
        except Exception:
            pass

    @staticmethod
    def _extract_request_code(url: str) -> str | None:
        try:
            parsed = urlparse(url)
            query_params = parse_qs(parsed.query)
            if 'code' in query_params:
                return query_params['code'][0]
            for key, values in query_params.items():
                for val in values:
                    if 'code=' in val:
                        return UpstoxClient._extract_request_code(val)
            return None
        except Exception:
            return None

    def _start_callback_server(self, code_queue: Queue[str]) -> Optional[HTTPServer]:
        client_ref = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                code = client_ref._extract_request_code(self.path)
                if code:
                    code_queue.put(code)
                    body = b"<h2>Upstox login successful. You can close this tab.</h2>"
                else:
                    body = b"<h2>Upstox callback received without code.</h2>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: Any) -> None:
                return

        try:
            server = HTTPServer(('127.0.0.1', 8090), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server
        except Exception:
            return None

    def ensure_instruments_daily(self) -> None:
        # Load Zebu/FlatTrade common files
        super().ensure_instruments_daily()

        # Handle Upstox specific CSV instruments file
        target_path = BASE_DIR / "instruments" / "upstox_instruments.csv"
        os.makedirs(target_path.parent, exist_ok=True)
        
        needs_download = False
        if not target_path.exists():
            needs_download = True
        else:
            mtime = datetime.fromtimestamp(os.path.getmtime(target_path), market_now().tzinfo)
            if market_now() - mtime > timedelta(days=1):
                needs_download = True

        if needs_download:
            print("[UPSTOX] upstox_instruments.csv missing or outdated. Downloading from Upstox assets CDN...", flush=True)
            try:
                url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
                r = requests.get(url, timeout=60, stream=True)
                if r.status_code == 200:
                    gz_path = target_path.with_suffix(".csv.gz")
                    with open(gz_path, "wb") as f:
                        shutil.copyfileobj(r.raw, f)
                    
                    # Decompress
                    with gzip.open(gz_path, "rb") as f_in:
                        with open(target_path, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    
                    # Cleanup gz
                    os.remove(gz_path)
                    print(f"[UPSTOX] Successfully downloaded and decompressed instrument master: {target_path}", flush=True)
                    self.load_upstox_instrument_map(force=True)
                else:
                    print(f"[UPSTOX WARNING] Failed to download Upstox instruments: HTTP {r.status_code}", flush=True)
            except Exception as e:
                print(f"[UPSTOX WARNING] Failed to download Upstox instruments: {e}", flush=True)

    def load_upstox_instrument_map(self, force: bool = False) -> None:
        if self.upstox_key_map and not force:
            return
            
        csv_path = BASE_DIR / "instruments" / "upstox_instruments.csv"
        if not csv_path.exists():
            return

        try:
            print("[UPSTOX] Parsing upstox_instruments.csv for key-token resolution mappings...", flush=True)
            df = pd.read_csv(csv_path, low_memory=False)
            
            # Fast vectorized parsing to avoid slow iterrows loop
            keys = [str(x) for x in df['instrument_key']]
            symbols = [str(x).upper() for x in df['tradingsymbol']]
            tokens = []
            for x in df['exchange_token']:
                if pd.isna(x):
                    tokens.append("")
                else:
                    try:
                        tokens.append(str(int(float(x))))
                    except Exception:
                        tokens.append(str(x))
            
            key_map = {}
            for tsym, ikey in zip(symbols, keys):
                if tsym in key_map:
                    existing = key_map[tsym]
                    if existing.startswith('NSE') and not ikey.startswith('NSE'):
                        pass
                    else:
                        key_map[tsym] = ikey
                else:
                    key_map[tsym] = ikey

            tsym_map = dict(zip(keys, symbols))
            
            token_map = {}
            token_to_key = {}
            
            for ikey, tok, tsym in zip(keys, tokens, symbols):
                if ikey and tok:
                    if tok.endswith('.0'):
                        tok = tok[:-2]
                    token_map[ikey] = tok
                    segment = ikey.split("|")[0]
                    exchange = {"NSE_EQ":"NSE","NSE_FO":"NFO","BSE_EQ":"BSE","BSE_FO":"BFO","NSE_INDEX":"NSE","BSE_INDEX":"BSE","MCX_FO":"MCX","NSE_CD":"CDS"}.get(segment)
                    if exchange:
                        token_to_key[f"{exchange}|{tok}"] = ikey
                if tsym.endswith('FUT'):
                    alt_f = tsym[:-3] + 'F'
                    key_map[alt_f] = ikey

            self.upstox_key_map = key_map
            self.upstox_tsym_map = tsym_map
            self.upstox_token_map = token_map
            self.upstox_token_to_key = token_to_key
            print(f"[UPSTOX] Loaded {len(self.upstox_key_map)} instrument resolution keys successfully.", flush=True)
        except Exception as e:
            logger.error(f"Failed loading upstox_instruments.csv: {e}")

    def shoonya_to_upstox_key(self, token: str) -> Optional[str]:
        indices = {"NSE|26000": "NSE_INDEX|Nifty 50", "NSE|26009": "NSE_INDEX|Nifty Bank",
                   "NSE|26037": "NSE_INDEX|Nifty Fin Services", "NSE|26074": "NSE_INDEX|NIFTY MID SELECT",
                   "NSE|26017": "NSE_INDEX|India VIX", "BSE|1": "BSE_INDEX|SENSEX", "BSE|12": "BSE_INDEX|BANKEX"}
        if token in indices:
            return indices[token]
        if token in self.upstox_token_to_key:
            return self.upstox_token_to_key[token]
        s = token.split('|')[-1].upper().strip()
        if not s:
            return None
        
        # Check token-to-key lookup (if s is numeric token)
        if s.isdigit():
            matches={key for ex_token,key in self.upstox_token_to_key.items() if ex_token.endswith("|"+s)}
            return next(iter(matches)) if len(matches)==1 else None
            
        # Hardcoded index mappings
        if s in {"NIFTY 50", "NIFTY_50", "NIFTY"}:
            return "NSE_INDEX|Nifty 50"
        if s in {"NIFTY BANK", "NIFTY_BANK", "BANKNIFTY"}:
            return "NSE_INDEX|Nifty Bank"
        if s in {"NIFTY FIN SERVICES", "FINNIFTY"}:
            return "NSE_INDEX|Nifty Fin Services"
        if s in {"MIDCPNIFTY", "NIFTY MIDCAP 50"}:
            return "NSE_INDEX|NIFTY MID SELECT"
        if s in {"SENSEX", "BSESN"}:
            return "BSE_INDEX|SENSEX"

        # Check direct lookup
        if s in self.upstox_key_map:
            return self.upstox_key_map[s]
            
        # Futures: 360ONE28JUL26F -> 360ONE26JULFUT
        if s.endswith('F') or s.endswith('FUT'):
            raw = s[:-1] if s.endswith('F') else s[:-3]
            m = re.match(r'^(.*?)(0[1-9]|[12][0-9]|3[01])([A-Z]{3})([0-9]{2})$', raw)
            if m:
                sym, day, month, year = m.groups()
                sym_clean = sym.replace('-', '')
                cand1 = f"{sym_clean}{year}{month}FUT"
                cand2 = f"{sym}{year}{month}FUT"
                ikey = self.upstox_key_map.get(cand1) or self.upstox_key_map.get(cand2)
                if ikey:
                    return ikey

        # Options: TVSMOTOR28JUL26C3800 -> TVSMOTOR26JUL3800CE
        m_opt = re.match(r'^(.*?)(0[1-9]|[12][0-9]|3[01])([A-Z]{3})([0-9]{2})([CP])([0-9.]+)$', s)
        if m_opt:
            sym, day, month, year, opt_type_char, strike = m_opt.groups()
            opt_type = 'CE' if opt_type_char == 'C' else 'PE'
            try:
                strike_val = float(strike)
                strike_str = str(int(strike_val)) if strike_val.is_integer() else str(strike_val)
            except Exception:
                strike_str = strike
            sym_clean = sym.replace('-', '')
            cand1 = f"{sym_clean}{year}{month}{strike_str}{opt_type}"
            cand2 = f"{sym}{year}{month}{strike_str}{opt_type}"
            ikey = self.upstox_key_map.get(cand1) or self.upstox_key_map.get(cand2)
            if ikey:
                return ikey

        return None

    def start_websocket(self) -> None:
        if not self._session_token or self._ws_feed_opened:
            return
            
        try:
            import upstox_client
            if not hasattr(upstox_client, 'MarketDataStreamerV3'):
                return

            
            # Setup configuration
            configuration = upstox_client.Configuration()
            configuration.access_token = self._session_token

            # Resolve keys for all currently subscribed symbols
            upstox_keys = set()
            for key in self._ws_subscribed:
                # key is exchange|token
                exchange, token = key.split("|", 1)
                ikey = self.shoonya_to_upstox_key(f"{exchange}|{token}")
                if ikey:
                    upstox_keys.add(ikey)
                    self.v3_key_to_req[ikey] = token
                    self.v3_key_to_req[ikey.replace("|", ":")] = token

            def on_open():
                self._ws_feed_opened = True

            def on_message(message):
                try:
                    data = message if isinstance(message, dict) else json.loads(message)
                    feeds = data.get("feeds", {})
                    for key_name, feed in feeds.items():
                        key_clean = key_name.replace(":", "|")
                        mff = feed.get("marketFF", {})
                        ltpc = feed.get("ltpc") or mff.get("ltpc", {})
                        
                        lp = None
                        cp = None
                        if ltpc:
                            lp = float(ltpc.get("ltp", 0.0))
                            cp = float(ltpc.get("cp", lp))
                            
                        ohlc_obj = feed.get("marketOHLC") or mff.get("marketOHLC", {})
                        open_px, high_px, low_px = lp, lp, lp
                        if ohlc_obj:
                            ohlc_list = ohlc_obj.get("ohlc", [])
                            if ohlc_list:
                                day_bar = ohlc_list[0]
                                open_px = float(day_bar.get("open", lp))
                                high_px = float(day_bar.get("high", lp))
                                low_px = float(day_bar.get("low", lp))

                        if lp is not None:
                            # Map key back to (exchange, token)
                            token_num = self.upstox_token_map.get(key_clean)
                            if not token_num:
                                # Fallback split
                                token_num = key_clean.split("|")[-1]

                            # Determine exchange
                            exch = "NSE"
                            if "BSE" in key_clean:
                                exch = "BSE"
                            elif "NFO" in key_clean or "NSE_FO" in key_clean:
                                exch = "NFO"
                            elif "BFO" in key_clean:
                                exch = "BFO"
                                
                            cache_key = (exch, token_num)
                            
                            # Build Shoonya-like quote dict
                            quote_dict = {
                                "lp": lp,
                                "bp1": round(lp - 0.05, 2),
                                "sp1": round(lp + 0.05, 2),
                                "h": high_px,
                                "l": low_px,
                                "o": open_px,
                                "c": cp if cp else lp,
                                "ti": 0.05
                            }
                            self._ws_quotes[cache_key] = quote_dict
                            self._ws_quote_time[cache_key] = time.monotonic()
                except Exception as ex:
                    logger.debug(f"[WS MSG ERROR] {ex}")

            def on_error(err):
                logger.error(f"[WS ERROR] {err}")
                if self.v3_streamer:
                    try:
                        self.v3_streamer.disconnect()
                    except Exception:
                        pass
                self.v3_streamer = None
                self._ws_feed_opened = False

            def on_close(code=None, reason=None):
                logger.info(f"[WS CLOSED] Code: {code}, Reason: {reason}")
                self._ws_feed_opened = False

            self.v3_streamer = upstox_client.MarketDataStreamerV3(
                upstox_client.ApiClient(configuration),
                list(upstox_keys),
                "ltpc"
            )
            self.v3_streamer.on("open", on_open)
            self.v3_streamer.on("message", on_message)
            self.v3_streamer.on("error", on_error)
            self.v3_streamer.on("close", on_close)

            threading.Thread(target=self.v3_streamer.connect, daemon=True).start()
            print("[UPSTOX] Connected to V3 Streamer websocket.", flush=True)
        except Exception as e:
            print(f"[UPSTOX WARNING] Could not start V3 Streamer: {e}", flush=True)

    def close_websocket(self) -> None:
        self._ws_feed_opened = False
        if self.v3_streamer:
            try:
                self.v3_streamer.disconnect()
            except Exception:
                pass
            self.v3_streamer = None

    def subscribe_ws(self, exchange: str, token: str) -> bool:
        key = f"{exchange.upper()}|{token.strip()}"
        if key in self._ws_subscribed:
            return True
        self._ws_subscribed.add(key)
        
        # Subscribe dynamically if websocket running
        if self._ws_feed_opened and self.v3_streamer:
            ikey = self.shoonya_to_upstox_key(f"{exchange}|{token}")
            if ikey:
                self.v3_key_to_req[ikey] = token
                self.v3_key_to_req[ikey.replace("|", ":")] = token
                try:
                    self.v3_streamer.subscribe([ikey], "ltpc")
                    return True
                except Exception:
                    pass
        return False

    def unsubscribe_ws(self, exchange: str, token: str) -> bool:
        key = f"{exchange.upper()}|{token.strip()}"
        if key in self._ws_subscribed:
            self._ws_subscribed.remove(key)
            if self._ws_feed_opened and self.v3_streamer:
                ikey = self.shoonya_to_upstox_key(f"{exchange}|{token}")
                if ikey:
                    try:
                        self.v3_streamer.unsubscribe([ikey])
                        return True
                    except Exception:
                        pass
        return False

    def search_token(self, exchange: str, symbol: str) -> str:
        # Returns token number by querying instrument CSV
        clean_sym = symbol.strip().upper()
        ikey = self.upstox_key_map.get(clean_sym)
        if ikey:
            return self.upstox_token_map.get(ikey, "")
        return ""

    def get_bars(
        self, 
        exchange: str, 
        symbol: str, 
        interval: int = 5, 
        start: Optional[datetime] = None, 
        end: Optional[datetime] = None
    ) -> list[dict]:
        # Fetch historical candles from Upstox REST API
        ikey = self.shoonya_to_upstox_key(f"{exchange}|{symbol}") or self.upstox_key_map.get(symbol.upper())
        if not ikey:
            raise RuntimeError("Exact Upstox historical instrument key unavailable")

        import urllib.parse
        safe_ikey = urllib.parse.quote(ikey)

        # Upstox API v2 only supports (1minute, 30minute, day, week, month)
        # Resample other timeframes (e.g. 3, 5, 10, 15, 60 minutes) from 1minute
        need_resample = False
        if interval in (1, 30):
            interval_name = "1minute" if interval == 1 else "30minute"
        else:
            interval_name = "1minute"
            need_resample = True

        end_date = end or market_now()
        start_date = start or (end_date - timedelta(days=10))

        end_str = end_date.strftime("%Y-%m-%d")
        start_str = start_date.strftime("%Y-%m-%d")

        today = market_now().date()
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._session_token}"}
        candles = []
        history_end = min(market_time(end_date).date(), today - timedelta(days=1))
        if market_time(start_date).date() <= history_end:
            url = f"https://api.upstox.com/v2/historical-candle/{safe_ikey}/{interval_name}/{history_end.isoformat()}/{start_str}"
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            candles.extend(response.json().get("data", {}).get("candles", []))
        if market_time(start_date).date() <= today <= market_time(end_date).date():
            url = f"https://api.upstox.com/v2/historical-candle/intraday/{safe_ikey}/{interval_name}"
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            candles.extend(response.json().get("data", {}).get("candles", []))
        if not candles:
            return []

        if need_resample:
            try:
                # Convert to DataFrame (candles are reversed in API response: oldest last)
                df_raw = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume", "oi"])
                df_raw["time"] = pd.to_datetime(df_raw["time"])
                df_raw = df_raw.set_index("time").sort_index()

                resampler_rule = f"{interval}min"
                df_resampled = df_raw.resample(resampler_rule, origin="start_day", offset="9h15min").agg({
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum"
                }).dropna()

                bars = []
                for ts, row in df_resampled.iterrows():
                    bars.append({
                        "time": market_time(ts.to_pydatetime()),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    })
                return sorted(bars, key=lambda r: r["time"])
            except Exception as ex:
                logger.error(f"[UPSTOX RESAMPLE ERROR] {ex}")

        # Standard direct mapping
        bars = []
        for c in reversed(candles):
            ts_str = c[0]
            try:
                dt_obj = market_time(pd.to_datetime(ts_str).to_pydatetime())
            except Exception:
                dt_obj = market_now()

            bars.append({
                "time": dt_obj,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        return sorted(bars, key=lambda r: r["time"])

    def get_quote(self, exchange: str, token: str, bypass_cache: bool = False) -> dict:
        self.ensure_connected()
        self.subscribe_ws(exchange,token)
        # Check cache/websocket first
        exch = exchange.upper()
        cache_key = (exch, token.strip())
        
        if not bypass_cache and self._ws_feed_opened:
            if cache_key in self._ws_quotes and time.monotonic()-self._ws_quote_time.get(cache_key,0)<=self.max_quote_age:
                return self._ws_quotes[cache_key]

        ikey = self.shoonya_to_upstox_key(f"{exch}|{token}")
        if not ikey:
            raise RuntimeError("Exact Upstox quote instrument key unavailable")

        req_key = ikey.replace("|", ":")
        url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={req_key}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session_token}"
        }
        
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"Upstox quote API error: {r.status_code} - {r.text}")

        res_data = r.json().get("data", {})
        qdata = res_data.get(req_key)
        if not qdata:
            raise RuntimeError(f"No quote data returned for key {req_key}")

        lp = qdata.get("last_price", 0.0)
        depth = qdata.get("depth", {})
        buy_list = depth.get("buy", [])
        sell_list = depth.get("sell", [])
        bp1 = buy_list[0].get("price", lp) if buy_list else lp
        sp1 = sell_list[0].get("price", lp) if sell_list else lp
        
        ohlc = qdata.get("ohlc", {})
        o = ohlc.get("open", lp)
        h = ohlc.get("high", lp)
        l = ohlc.get("low", lp)
        c = ohlc.get("close", lp)

        mapped_quote = {
            "lp": lp,
            "bp1": bp1,
            "sp1": sp1,
            "o": o,
            "h": h,
            "l": l,
            "c": c,
            "ti": 0.05
        }
        
        # Populate cache
        self._ws_quotes[cache_key] = mapped_quote
        self._ws_quote_time[cache_key] = time.monotonic()
        return mapped_quote

    def place_order(
        self,
        *,
        exchange: str,
        tradingsymbol: str,
        side: str,
        quantity: int,
        product_type: str,
        price_type: str,
        price: float,
        trigger_price: Optional[float] = None,
        confirm_live: bool = False,
        client_order_id: str | None = None
    ) -> dict:
        if price_type != "LMT":
            raise ValueError("Only LIMIT orders are supported for algo execution")
        if price <= 0 or quantity <= 0:
            raise ValueError("Limit price and quantity must be positive")
        is_paper = not (confirm_live and self.settings.live_trading_enabled)
        if is_paper:
            # Paper execution
            order_id = f"UPSTOX_SIM_{int(time.time() * 1000)}"
            print(f"[PAPER ORDER] Placed mock order {order_id} for {side} {quantity} {tradingsymbol} @ {price}", flush=True)
            return {
                "status": "success",
                "order_id": order_id,
                "norenordno": order_id,
                "data": {"order_id": order_id}
            }

        # Live execution
        ikey = self.shoonya_to_upstox_key(tradingsymbol)
        if not ikey:
            raise RuntimeError("Exact Upstox order instrument key unavailable")

        url = "https://api.upstox.com/v2/order/place"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session_token}"
        }

        # product: "I" if MIS ("M"), "D" if CNC/NRML ("C")
        if product_type not in {"I", "M", "D"}:
            raise ValueError("Unsupported product type")
        product = "I" if product_type == "I" else "D"
        order_type = "LIMIT"

        payload = {
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": price,
            "tag": client_order_id or "TVIndicatorAlgo",
            "instrument_token": ikey,
            "order_type": order_type,
            "transaction_type": side.upper(),
            "disclosed_quantity": 0,
            "trigger_price": trigger_price or 0.0,
            "is_amo": False
        }

        r = requests.post(url, json=payload, headers=headers, timeout=10)
        res_json = r.json()

        if r.status_code != 200 or res_json.get("status") == "error":
            err_msg = "Unknown error"
            errors = res_json.get("errors", [])
            if errors:
                err_msg = ", ".join(e.get("message", "") for e in errors if e.get("message"))
            elif res_json.get("message"):
                err_msg = res_json["message"]
            raise RuntimeError(f"Upstox place order failed: {err_msg}")

        order_id = res_json["data"]["order_id"]
        return {
            "status": "success",
            "order_id": order_id,
            "norenordno": order_id,
            "data": res_json["data"]
        }

    def cancel_order(self, order_id: str) -> dict:
        if "UPSTOX_SIM" in order_id:
            # Paper cancel
            return {"status": "success", "message": "Simulated order cancelled"}

        url = f"https://api.upstox.com/v2/order/cancel?order_id={order_id}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session_token}"
        }

        r = requests.delete(url, headers=headers, timeout=10)
        res_json = r.json()

        if r.status_code != 200 or res_json.get("status") == "error":
            err_msg = "Unknown error"
            errors = res_json.get("errors", [])
            if errors:
                err_msg = ", ".join(e.get("message", "") for e in errors if e.get("message"))
            elif res_json.get("message"):
                err_msg = res_json["message"]
            return {"status": "error", "message": err_msg}

        return {"status": "success", "message": "Order cancelled"}

    def get_order_book(self) -> list[dict]:
        url = "https://api.upstox.com/v2/order/retrieve-all"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session_token}"
        }

        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"Upstox read failed: HTTP {r.status_code}")

        res_json = r.json()
        orders = res_json.get("data", [])
        
        # Map Upstox order fields to Shoonya order book expectations
        # Expected fields: 'norenordno', 'status', 'rejreason', 'avgprc', 'flqty'
        mapped_orders = []
        for o in orders:
            order_id = o.get("order_id")
            # Map Upstox status (e.g. 'complete', 'rejected', 'open') to Shoonya upper state
            status = str(o.get("status", "")).upper()
            if status == "COMPLETE":
                status = "COMPLETE"
            elif status == "REJECTED":
                status = "REJECTED"
            
            # Map product
            prod = "M" if o.get("product") == "D" else "I"

            mapped_orders.append({
                "norenordno": order_id,
                "orderno": order_id,
                "order_id": order_id,
                "status": status,
                "remarks": o.get("tag"),
                "rejreason": o.get("status_message") or "",
                "avgprc": str(o.get("average_price", 0.0)),
                "flqty": str(o.get("filled_quantity", 0)),
                "qty": str(o.get("quantity", 0)),
                "prd": prod,
                "side": o.get("transaction_type"),
                "tsym": self.upstox_tsym_map.get(o.get("instrument_token"), o.get("tradingsymbol", ""))
            })
        return mapped_orders

    def get_positions(self) -> list[dict]:
        url = "https://api.upstox.com/v2/portfolio/short-term-positions"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session_token}"
        }

        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"Upstox read failed: HTTP {r.status_code}")

        res_json = r.json()
        positions = res_json.get("data", [])
        
        # Map Upstox position fields to Shoonya expectations
        # Expected: 'tsym', 'prd', 'netqty', 'netavgprc', 'urmtom', 'rpnl'
        mapped_positions = []
        for p in positions:
            mapped_positions.append({
                "exch": p.get("exchange"),
                "tsym": p.get("tradingsymbol"),
                "prd": "M" if p.get("product") == "D" else "I",
                "netqty": str(p.get("quantity", 0)),
                "netavgprc": str(p.get("average_price", 0.0)),
                "urmtom": str(p.get("unrealized_pnl", 0.0)),
                "rpnl": str(p.get("realized_pnl", 0.0))
            })
        return mapped_positions

    def single_order_history(self, order_id: str) -> Any:
        if "UPSTOX_SIM" in order_id:
            # Paper simulation fallback
            return {
                "status": "COMPLETE",
                "norenordno": order_id,
                "stat": "Ok",
                "avgprc": "0.0",
                "flqty": "0"
            }

        url = f"https://api.upstox.com/v2/order/history?order_id={order_id}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session_token}"
        }

        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None

        res_json = r.json()
        history = res_json.get("data", [])
        
        if not history:
            return None
            
        # Return the latest state of the order
        latest = history[0]
        status = str(latest.get("status", "")).upper()
        
        return {
            "status": status,
            "stat": "Ok" if status != "REJECTED" else "Not_Ok",
            "norenordno": order_id,
            "rejreason": latest.get("status_message") or "",
            "avgprc": str(latest.get("average_price", 0.0)),
            "flqty": str(latest.get("filled_quantity", 0)),
            "qty": str(latest.get("quantity", 0)),
            "side": latest.get("transaction_type")
        }


def start_persistent_callback_server() -> None:
    """Bridge registered loopback redirects to the state-validating main callback."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            values = {k: query[k][0] for k in ("code", "state", "error") if k in query}
            self.send_response(302)
            self.send_header("Location", "http://localhost:8005/upstox/callback?" + urlencode(values))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        def log_message(self, *_):
            pass
    try:
        server = HTTPServer(("127.0.0.1", 8090), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        logger.warning("Optional OAuth redirect bridge port 8090 is unavailable")
