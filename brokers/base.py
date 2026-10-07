from utils.clock import market_now, market_time, candle_start
from datetime import datetime
import time
import secrets
import hmac
import threading
from typing import Any
import pandas as pd

from config.settings import Settings, BASE_DIR
from data.instruments import InstrumentMaster

from config.constants import INDEX_TOKENS


class BaseBrokerClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api: Any = None
        self.order_event = threading.Event()
        self._order_update_lock = threading.Lock()
        self._order_updates = {}
        self._oauth_lock = threading.Lock()
        self._oauth_state = None
        self._oauth_deadline = 0.0
        self.connected = False
        self.last_error: str | None = None
        self._session_token: str | None = None  # Persisted session token
        self.instruments = InstrumentMaster.create(BASE_DIR)
        
        # Caches
        self._index_quotes_cache: dict[str, dict[str, Any]] = {}
        self._quote_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._quote_cache_time: dict[tuple[str, str], float] = {}
        self._token_cache: dict[tuple[str, str], str] = {}
        
        # WebSocket States
        self._ws_quotes: dict[tuple[str, str], dict[str, Any]] = {}
        self._ws_quote_time: dict[tuple[str, str], float] = {}
        self.max_quote_age = 3.0
        self._ws_subscribed: set[str] = set()
        self._ws_feed_opened = False

    def publish_order_update(self, update):
        oid = str(update.get("norenordno") or update.get("order_id") or "")
        if not oid:
            return
        with self._order_update_lock:
            # Bound memory for unsolicited account-wide order updates.
            if len(self._order_updates)>=1024 and oid not in self._order_updates:
                self._order_updates.pop(next(iter(self._order_updates)))
            self._order_updates[oid]=dict(update)
        self.order_event.set()

    def take_order_update(self, oid):
        with self._order_update_lock:
            return self._order_updates.pop(str(oid),None)

    def connect_with_token(self) -> bool:
        raise NotImplementedError()

    def connect_auto_oauth(self, timeout_seconds: int = 180) -> bool:
        raise NotImplementedError()

    def _begin_oauth(self) -> str:
        with self._oauth_lock:
            self._oauth_state = secrets.token_urlsafe(32)
            self._oauth_deadline = time.monotonic() + 300
            return self._oauth_state

    def _consume_oauth_state(self, state: str) -> bool:
        with self._oauth_lock:
            valid = bool(state and self._oauth_state and time.monotonic() < self._oauth_deadline and hmac.compare_digest(state, self._oauth_state))
            if valid:
                self._oauth_state = None
            else:
                self.last_error = "OAuth state missing, expired, or invalid; start login again"
            return valid

    def connect_oauth_code(self, auth_code: str, state: str = "") -> bool:
        raise NotImplementedError()

    def status(self) -> dict:
        raise NotImplementedError()

    def start_websocket(self) -> None:
        raise NotImplementedError()

    def _validate_token(self) -> bool:
        if not self.api:
            return False
        try:
            test = self.api.get_limits()
            if isinstance(test, dict) and test.get("stat") == "Ok":
                return True
            return False
        except Exception:
            return False

    def ensure_instruments_daily(self) -> None:
        try:
            self.instruments.ensure_daily()
        except Exception as exc:
            self.last_error = f"Instrument download failed: {exc}"

    def instrument_metadata(self, exchange: str, symbol: str, underlying: str) -> dict[str, Any]:
        if not self.instruments.frames:
            self.instruments.load_cached()
        return self.instruments.metadata(exchange, symbol, underlying)

    def ensure_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("Broker disconnected; reconnect explicitly from the login panel")

    def close_websocket(self) -> None:
        """Close the WebSocket connection if it is open."""
        self._ws_feed_opened = False
        if self.api:
            try:
                self.api.close_websocket()
                print("[WS] WebSocket connection closed explicitly", flush=True)
            except Exception as e:
                print(f"[WS] Error closing WebSocket: {e}", flush=True)

    def mark_session_expired(self) -> None:
        """Call when broker API response signals an expired/invalid session."""
        if self.connected:
            print("[AUTH] Session marked as expired — will re-authenticate on next API call.", flush=True)
        self.connected = False
        self._session_token = None
        self._ws_feed_opened = False
        self.close_websocket()
        self.api = None

    def subscribe_ws(self, exchange: str, token: str) -> bool:
        self.ensure_connected()
        if not self.connected or not self.api:
            return False

        key = f"{exchange.upper()}|{token}"
        if key in self._ws_subscribed:
            return True

        try:
            self.api.subscribe([key])
            self._ws_subscribed.add(key)
            print(f"[WS] Subscribed to {key}", flush=True)
            return True
        except Exception as e:
            print(f"[WS] Subscribe error for {key}: {e}", flush=True)
            return False

    def unsubscribe_ws(self, exchange: str, token: str) -> bool:
        self.ensure_connected()
        if not self.connected or not self.api:
            return False

        key = f"{exchange.upper()}|{token}"
        if key not in self._ws_subscribed:
            return True

        try:
            self.api.unsubscribe([key])
            self._ws_subscribed.discard(key)
            print(f"[WS] Unsubscribed from {key}", flush=True)
            return True
        except Exception as e:
            print(f"[WS] Unsubscribe error for {key}: {e}", flush=True)
            return False

    def search_token(self, exchange: str, symbol: str) -> str:
        self.ensure_connected()
        clean_sym = symbol.strip().upper()
        index_token = INDEX_TOKENS.get(clean_sym)
        if index_token:
            return index_token
            
        cache_key = (exchange.upper(), symbol.strip().upper())
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]
            
        response = self.api.searchscrip(exchange=exchange, searchtext=symbol)
        values = response.get("values", []) if isinstance(response, dict) else []
        if not values:
            raise RuntimeError(f"No token found for {exchange}:{symbol}")
            
        token = str(values[0].get("token"))
        self._token_cache[cache_key] = token
        return token

    def get_bars(
        self,
        exchange: str,
        symbol: str,
        interval: int = 5,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]:
        self.ensure_connected()
        clean_exch = exchange.upper()
        clean_sym = symbol.strip().upper()
        if clean_sym in INDEX_TOKENS:
            broker_exchange = "BSE" if clean_sym in {"SENSEX", "BANKEX"} else "NSE"
        else:
            broker_exchange = "NSE" if clean_exch == "INDICES" else exchange
            
        token = self.search_token("INDICES" if clean_exch == "INDICES" else broker_exchange, symbol)
        kwargs: dict[str, Any] = {"exchange": broker_exchange, "token": token, "interval": interval}
        if start:
            kwargs["starttime"] = int(market_time(start).timestamp())
        if end:
            kwargs["endtime"] = int(market_time(end).timestamp())
        
        response = self.api.get_time_price_series(**kwargs)
        if isinstance(response, dict) and response.get("stat") == "Not_Ok":
            err_msg = response.get("emsg", "Historical data series failed")
            if any(k in (err_msg or "").lower() for k in (
                "session", "invalid", "token", "expired", "unauthor", "not logged", "login"
            )):
                self.mark_session_expired()
            raise RuntimeError(err_msg)

        if response is None:
            # NorenApi returns None if broker response is not a list (e.g. error dict or session expired)
            if not self._validate_token():
                self.mark_session_expired()
                raise RuntimeError("Broker session expired or invalid while fetching historical candles. Please re-authenticate.")
            return []

        rows = []
        for item in response or []:
            if isinstance(item, dict) and item.get("time"):
                try:
                    rows.append(
                        {
                            "time": market_time(pd.to_datetime(item.get("time"), dayfirst=True).to_pydatetime()),
                            "open": float(item.get("into", 0)),
                            "high": float(item.get("inth", 0)),
                            "low": float(item.get("intl", 0)),
                            "close": float(item.get("intc", 0)),
                            "volume": float(item.get("intv") or item.get("v") or 0),
                        }
                    )
                except Exception:
                    continue
        return sorted(rows, key=lambda r: r["time"])

    def get_quote(self, exchange: str, token: str, bypass_cache: bool = False) -> dict:
        self.ensure_connected()
        
        # Auto-subscribe to WebSocket stream if feed is active
        if self._ws_feed_opened and not bypass_cache:
            self.subscribe_ws(exchange, token)

        if not bypass_cache:
            if self._ws_feed_opened:
                key = (exchange.upper(), token.strip())
                if key in self._ws_quotes and time.monotonic()-self._ws_quote_time.get(key,0) <= self.max_quote_age:
                    return self._ws_quotes[key]
                    
            now_ts = time.time()
            cache_key = (exchange.upper(), token.strip())
            is_index = token in INDEX_TOKENS.values()
            cache_duration = 2.5 if is_index else 0.8
            
            if cache_key in self._quote_cache:
                last_time = self._quote_cache_time.get(cache_key, 0.0)
                if (now_ts - last_time) < cache_duration:
                    return self._quote_cache[cache_key]

        if hasattr(self.api, "get_quotes"):
            response = self.api.get_quotes(exchange=exchange, token=token)
        else:
            response = self.api.get_security_info(exchange=exchange, token=token)
            
        if not response or response.get("stat") == "Not_Ok":
            err_msg = response.get("emsg", "Quote fetch failed") if response else "Quote fetch failed"
            if any(k in (err_msg or "").lower() for k in (
                "session", "invalid", "token", "expired", "unauthor", "not logged", "login"
            )):
                self.mark_session_expired()
            raise RuntimeError(err_msg)
            
        cache_key = (exchange.upper(), token.strip())
        self._quote_cache[cache_key] = response
        self._quote_cache_time[cache_key] = time.time()
        return response

    @staticmethod
    def quote_ltp(quote: dict) -> float | None:
        import math
        value = quote.get("lp") if quote.get("lp") is not None else quote.get("ltp")
        value = float(value) if value is not None else None
        return value if value is not None and math.isfinite(value) and value > 0 else None

    @staticmethod
    def _quote_number(quote: dict, keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = quote.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    def quote_snapshot(self, exchange: str, token: str) -> dict[str, Any]:
        quote = self.get_quote(exchange, token, bypass_cache=True)
        ltp = self.quote_ltp(quote)
        best_buy = self._quote_number(quote, ("bp1", "best_buy", "best_bid", "bid", "b1", "bp"))
        best_sell = self._quote_number(quote, ("sp1", "best_sell", "best_ask", "ask", "a1", "sp"))
        tick_size = self._quote_number(quote, ("ti", "tick_size", "ticksize", "tick"))
        return {
            "raw": quote,
            "ltp": ltp,
            "best_buy": best_buy,
            "best_sell": best_sell,
            "tick_size": tick_size or 0.05,
        }

    def get_order_book(self) -> Any:
        self.ensure_connected()
        return self.api.get_order_book()

    def get_trade_book(self) -> Any:
        self.ensure_connected()
        return self.api.get_trade_book()

    def get_positions(self) -> Any:
        self.ensure_connected()
        return self.api.get_positions()

    def single_order_history(self, order_id: str) -> Any:
        self.ensure_connected()
        return self.api.single_order_history(orderno=order_id)

    def get_index_quotes(self) -> list[dict[str, Any]]:
        quotes = []
        for name in ("NIFTY", "BANKNIFTY", "SENSEX", "BANKEX"):
            token = INDEX_TOKENS[name]
            exchange = "BSE" if name in {"SENSEX", "BANKEX"} else "NSE"
            if not self.connected:
                fallback = self._index_quotes_cache.get(
                    name,
                    {
                        "name": name,
                        "token": token,
                        "ltp": None,
                        "change": None,
                        "change_percent": None,
                        "previous_close": None,
                        "raw": None,
                    }
                )
                quotes.append(fallback)
                continue
            try:
                quote = self.get_quote(exchange, token)
                ltp = self.quote_ltp(quote)
                previous_close = self._quote_number(quote, ("c", "close", "prev_close", "previous_close", "pc"))
                change = ltp - previous_close if ltp is not None and previous_close not in (None, 0) else None
                change_percent = change * 100 / previous_close if change is not None and previous_close else None
                data = {
                    "name": name,
                    "token": token,
                    "ltp": ltp,
                    "change": change,
                    "change_percent": change_percent,
                    "previous_close": previous_close,
                    "raw": quote,
                }
                if ltp is not None:
                    self._index_quotes_cache[name] = data
                quotes.append(data)
            except Exception as e:
                fallback = self._index_quotes_cache.get(
                    name,
                    {
                        "name": name,
                        "token": token,
                        "ltp": None,
                        "change": None,
                        "change_percent": None,
                        "previous_close": None,
                        "raw": None,
                        "error": str(e),
                    }
                )
                quotes.append(dict(fallback,stale=True,error=str(e)))
        return quotes

    def get_single_index_quote(self, name: str) -> dict[str, Any] | None:
        self.ensure_connected()
        normalized_name = name.upper()
        token = INDEX_TOKENS.get(normalized_name)
        if not token:
            for key, val in INDEX_TOKENS.items():
                if key in normalized_name or normalized_name in key:
                    token = val
                    normalized_name = key
                    break
        if not token:
            return None
            
        if not self.connected:
            return self._index_quotes_cache.get(
                normalized_name,
                {
                    "name": normalized_name,
                    "token": token,
                    "ltp": None,
                    "change": None,
                    "change_percent": None,
                    "previous_close": None,
                    "raw": None,
                }
            )
            
        try:
            exchange = "BSE" if normalized_name in {"SENSEX", "BANKEX"} else "NSE"
            quote = self.get_quote(exchange, token)
            ltp = self.quote_ltp(quote)
            previous_close = self._quote_number(quote, ("c", "close", "prev_close", "previous_close", "pc"))
            change = ltp - previous_close if ltp is not None and previous_close not in (None, 0) else None
            change_percent = change * 100 / previous_close if change is not None and previous_close else None
            data = {
                "name": normalized_name,
                "token": token,
                "ltp": ltp,
                "change": change,
                "change_percent": change_percent,
                "previous_close": previous_close,
                "raw": quote,
            }
            if ltp is not None:
                self._index_quotes_cache[normalized_name] = data
            return data
        except Exception as e:
            fallback = self._index_quotes_cache.get(
                normalized_name,
                {
                    "name": normalized_name,
                    "token": token,
                    "ltp": None,
                    "change": None,
                    "change_percent": None,
                    "previous_close": None,
                    "raw": None,
                    "error": str(e),
                }
            )
            return dict(fallback,stale=True,error=str(e))

    def resolve_spot(self, exchange: str, underlying: str) -> dict:
        self.ensure_connected()
        broker_exchange = "NSE" if exchange.upper() == "INDICES" else exchange
        response = self.api.searchscrip(exchange=broker_exchange, searchtext=underlying)
        values = response.get("values", []) if isinstance(response, dict) else []
        if not values:
            raise RuntimeError(f"No spot instrument found for {broker_exchange}:{underlying}")
        matches=[v for v in values if str(v.get("tsym") or v.get("tradingsymbol") or "").upper() in {underlying.upper(),underlying.upper()+"-EQ"}]
        if len(matches)!=1:
            raise RuntimeError("Spot search did not identify one exact instrument")
        selected = matches[0]
        return {
            "exchange": broker_exchange,
            "tradingsymbol": selected.get("tsym") or selected.get("tradingsymbol") or underlying,
            "token": str(selected.get("token")),
            "raw": selected,
        }

    def resolve_future(self, underlying: str, exchange: str = "NFO", expiry: str = "CURRENT_MONTH") -> dict:
        contract = self.instruments.resolve_future(underlying,exchange,expiry)
        if not contract:
            raise RuntimeError("Exact future contract unavailable in verified instrument master; refresh instruments")
        return contract

    def resolve_option(self, underlying: str, strike: int, option_type: str, expiry: str = "CURRENT_WEEK", exchange: str = "NFO") -> dict:
        contract = self.instruments.resolve_option(underlying,strike,option_type,expiry,exchange)
        if not contract:
            raise RuntimeError("Exact option contract unavailable in verified instrument master; refresh instruments")
        return contract

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
        trigger_price: float | None,
        confirm_live: bool,
        client_order_id: str | None = None,
    ) -> dict:
        if price_type != "LMT":
            raise ValueError("Only LIMIT orders are supported for algo execution")
        if price <= 0 or quantity <= 0:
            raise ValueError("Limit price and quantity must be positive")
        if not self.settings.live_trading_enabled or not confirm_live:
            return {
                "paper": True,
                "message": "Live order blocked. Enable LIVE_TRADING_ENABLED and send confirm_live=true.",
                "order": {
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "side": side,
                    "quantity": quantity,
                    "product_type": product_type,
                    "price_type": price_type,
                    "price": price,
                    "trigger_price": trigger_price,
                },
            }

        self.ensure_connected()
        response = self.api.place_order(
            buy_or_sell="B" if side == "BUY" else "S",
            product_type=product_type,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            discloseqty=0,
            price_type=price_type,
            price=price,
            trigger_price=trigger_price,
            retention="DAY",
            remarks=client_order_id or "nse-tools-python",
        )
        if response:
            return response
        raise RuntimeError("Order placement failed")

    def cancel_order(self, order_id: str) -> dict:
        if not self.settings.live_trading_enabled:
            return {
                "paper": True,
                "message": "Live order cancellation blocked. Enable LIVE_TRADING_ENABLED.",
            }

        self.ensure_connected()
        response = self.api.cancel_order(orderno=order_id)
        if response:
            return response
        raise RuntimeError("Order cancellation failed")
