import re
import sys
from io import StringIO
from typing import Any

# Reconfigure stdout/stderr streams to prevent Windows 'charmap' encoding errors when third-party libraries print emojis
for stream in (sys.stdout, sys.stderr):
    if stream:
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config.settings import BASE_DIR, get_settings, reload_settings, save_token_to_env
from execution.runner import IntradayRunner
from models.schemas import (
    ApiResponse,
    BacktestRequest,
    CsvPayload,
    IntradayRunRequest,
    OrderAdjustRequest,
    OrderRequest,
    ZebuBarsRequest,
    ManualTradeActionRequest,
    ManualTradeCancelRequest,
)
from strategy.engine import run_strategy, normalize_bars
from brokers.base import BaseBrokerClient

# ==============================================================================
# SECTION 1: IMPORTS & API INITIALIZATION
# ==============================================================================

app = FastAPI(
    title="NSE Tools Python Algo - Multi-Broker Refactored Edition",
    version="0.2.0",
    description="Production-grade local API backend for Running Pine-based intraday options automation."
)

settings = get_settings()
broker_client: BaseBrokerClient | None = None


def get_active_client() -> BaseBrokerClient:
    global broker_client
    if broker_client is None:
        curr_settings = get_settings()
        if curr_settings.active_broker == "flattrade":
            from brokers.flattrade.client import FlatTradeClient
            broker_client = FlatTradeClient(curr_settings)
        elif curr_settings.active_broker == "upstox":
            from brokers.upstox.client import UpstoxClient
            broker_client = UpstoxClient(curr_settings)
        else:
            from brokers.zebu.client import ZebuClient
            broker_client = ZebuClient(curr_settings)
        # Bind to runner
        runner.broker = broker_client
    return broker_client


# Instantiate runner — broker client is initialized lazily on first request
runner = IntradayRunner(None)

# Mount local static file handler for index, styles, and script
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ==============================================================================
# SECTION 2: FRONTEND INDEX ROUTE
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve the main web UI panel page HTML content."""
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


# ==============================================================================
# SECTION 3: SYSTEM CONNECTION & STATE CHANNELS
# ==============================================================================

class ConnectRequest(BaseModel):
    broker: str


@app.post("/api/connect")
def connect_broker(req: ConnectRequest) -> ApiResponse:
    """Establish session to chosen broker (zebu, flattrade, or upstox) and save to settings."""
    global broker_client
    broker_name = req.broker.lower()
    if broker_name not in ("zebu", "flattrade", "upstox"):
        raise HTTPException(status_code=400, detail=f"Unsupported broker: {broker_name}")

    # Close any existing active connection
    if broker_client:
        try:
            broker_client.close_websocket()
        except Exception:
            pass

    # Set active broker in configuration (.env)
    save_token_to_env(broker=broker_name)  # No token passed, only updates the ACTIVE_BROKER key

    # Reload configurations
    curr_settings = reload_settings()

    # Re-instantiate broker client
    if broker_name == "flattrade":
        from brokers.flattrade.client import FlatTradeClient
        broker_client = FlatTradeClient(curr_settings)
    elif broker_name == "upstox":
        from brokers.upstox.client import UpstoxClient
        broker_client = UpstoxClient(curr_settings)
    else:
        from brokers.zebu.client import ZebuClient
        broker_client = ZebuClient(curr_settings)

    runner.broker = broker_client

    # Connect client
    ok = broker_client.connect_with_token()
    if not ok:
        ok = broker_client.connect_auto_oauth()
    if ok:
        broker_client.ensure_instruments_daily()

    return ApiResponse(
        ok=ok,
        data=broker_client.status(),
        error=broker_client.last_error if not ok else None
    )


@app.post("/api/disconnect")
def disconnect_broker() -> ApiResponse:
    """Log out of active broker and reset sessions."""
    global broker_client
    if broker_client:
        try:
            broker_client.close_websocket()
        except Exception:
            pass
        broker_client.mark_session_expired()
        active_broker = broker_client.settings.active_broker
        try:
            save_token_to_env(token="placeholder", broker=active_broker, suser_token="placeholder")
        except Exception:
            pass
        broker_client = None
    runner.broker = None
    return ApiResponse(ok=True, data={"connected": False})


@app.get("/upstox/callback", response_class=HTMLResponse)
def upstox_callback(code: str = None, error: str = None) -> HTMLResponse:
    """Handle the OAuth authorization callback from Upstox API directly on port 8005."""
    if error:
        return HTMLResponse(content=f"""
        <html>
        <head><title>Upstox Login Failed</title></head>
        <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
            <div style="display: inline-block; padding: 30px; border: 1px solid #ccc; border-radius: 8px; background: #fce8e6;">
                <h2 style="color: #c5221f;">&#10060; Upstox Login Failed</h2>
                <p>Error code: {error}</p>
            </div>
        </body>
        </html>
        """, status_code=400)

    if not code:
        return HTMLResponse(content="""
        <html>
        <head><title>Upstox Login Failed</title></head>
        <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
            <div style="display: inline-block; padding: 30px; border: 1px solid #ccc; border-radius: 8px; background: #fce8e6;">
                <h2 style="color: #c5221f;">&#10060; Upstox Login Failed</h2>
                <p>No authorization code received.</p>
            </div>
        </body>
        </html>
        """, status_code=400)

    # Exchange the code
    try:
        global broker_client
        curr_settings = reload_settings()

        # Instantiate Upstox client
        from brokers.upstox.client import UpstoxClient
        broker_client = UpstoxClient(curr_settings)

        ok = broker_client.connect_oauth_code(code)
        if ok:
            runner.broker = broker_client
            return HTMLResponse(content="""
            <html>
            <head><title>Upstox Login Successful</title></head>
            <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                <div style="display: inline-block; padding: 30px; border: 1px solid #ccc; border-radius: 8px; background: #e6f4ea;">
                    <h2 style="color: #137333;">&#10003; Upstox Login Successful!</h2>
                    <p>Session token generated and validated successfully.</p>
                    <p>Redirecting you back to your Trade Terminal...</p>
                    <script>
                        setTimeout(function() {
                            window.location.href = "/";
                        }, 2000);
                    </script>
                </div>
            </body>
            </html>
            """)
        else:
            return HTMLResponse(content=f"""
            <html>
            <head><title>Upstox Login Failed</title></head>
            <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                <div style="display: inline-block; padding: 30px; border: 1px solid #ccc; border-radius: 8px; background: #fce8e6;">
                    <h2 style="color: #c5221f;">&#10060; Upstox Login Failed</h2>
                    <p>Failed to establish session: {broker_client.last_error}</p>
                </div>
            </body>
            </html>
            """, status_code=400)
    except Exception as exc:
        return HTMLResponse(content=f"""
        <html>
        <head><title>Upstox Login Failed</title></head>
        <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
            <div style="display: inline-block; padding: 30px; border: 1px solid #ccc; border-radius: 8px; background: #fce8e6;">
                <h2 style="color: #c5221f;">&#10060; Upstox Login Exception</h2>
                <p>{exc}</p>
            </div>
        </body>
        </html>
        """, status_code=500)


@app.get("/api/status")
def status() -> ApiResponse:
    """Retrieve current Broker Client connection status and credential state."""
    client = get_active_client()
    return ApiResponse(ok=True, data=client.status())


@app.post("/api/settings/reload")
def settings_reload() -> ApiResponse:
    """Reload environment variables from .env and clear cached credentials settings."""
    global broker_client
    curr_settings = reload_settings()
    if broker_client:
        broker_client.settings = curr_settings
    else:
        get_active_client()
    return ApiResponse(ok=True, data=get_active_client().status())


@app.get("/api/index-quotes")
def index_quotes() -> ApiResponse:
    """Poll live spot quotes for NSE index benchmarks (NIFTY/BANKNIFTY)."""
    try:
        client = get_active_client()
        return ApiResponse(ok=True, data=client.get_index_quotes())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/instruments/metadata")
def instruments_metadata(exchange: str = "NSE", symbol: str = "Option", underlying: str = "NIFTY") -> ApiResponse:
    """Query current lot size, strike intervals, and expiries list for a given derivative contract type."""
    try:
        client = get_active_client()
        return ApiResponse(ok=True, data=client.instrument_metadata(exchange, symbol, underlying))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/instruments/underlyings")
def instruments_underlyings(exchange: str = "NSE") -> ApiResponse:
    """Query unique list of underlying symbols for a given exchange."""
    try:
        exch = exchange.upper()
        client = get_active_client()
        frame = client.instruments.frames.get(exch)
        if frame is None or frame.empty:
            return ApiResponse(ok=True, data=[])

        if exch == "BFO" and "TradingSymbol" in frame.columns:
            raw = frame["TradingSymbol"].dropna().unique().tolist()
            extracted: set[str] = set()
            for ts in raw:
                m = re.match(r"^([A-Za-z]+)", str(ts))
                if m:
                    extracted.add(m.group(1).upper())
            symbols = sorted(extracted)
        elif "Symbol" in frame.columns:
            raw_symbols = frame["Symbol"].dropna().unique().tolist()
            symbols = sorted([str(s) for s in raw_symbols if s])
        else:
            symbols = []

        return ApiResponse(ok=True, data=symbols)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/instruments/refresh")
def instruments_refresh() -> ApiResponse:
    """Force local download and cache reconstruction of exchange symbol files for the current trading day."""
    try:
        client = get_active_client()
        client.ensure_instruments_daily()
        return ApiResponse(ok=True, data=client.status())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/zebu/connect")
def legacy_zebu_connect() -> ApiResponse:
    """Legacy endpoint mapping for backwards compatibility."""
    client = get_active_client()
    try:
        ok = client.connect_with_token()
        if not ok:
            ok = client.connect_auto_oauth()
        if ok:
            client.ensure_instruments_daily()
        return ApiResponse(ok=ok, data=client.status(), error=client.last_error if not ok else None)
    except Exception as exc:
        return ApiResponse(ok=False, data=client.status(), error=str(exc))


@app.post("/api/broker/oauth-code/{auth_code}")
def broker_oauth(auth_code: str) -> ApiResponse:
    """Establish connection session with a manual callback authorization code."""
    client = get_active_client()
    try:
        ok = client.connect_oauth_code(auth_code)
        if ok:
            client.ensure_instruments_daily()
        return ApiResponse(ok=ok, data=client.status(), error=None if ok else client.last_error)
    except Exception as exc:
        return ApiResponse(ok=False, data=client.status(), error=str(exc))


@app.post("/api/zebu/oauth-code/{auth_code}")
def legacy_zebu_oauth(auth_code: str) -> ApiResponse:
    """Legacy callback mapping."""
    return broker_oauth(auth_code)


# ==============================================================================
# SECTION 4: BACKTESTING & CSV STRATEGY ENGINES
# ==============================================================================

@app.post("/api/backtest")
def backtest(request: BacktestRequest) -> ApiResponse:
    """Run indicator trend break signals over a list of provided historical bar ticks."""
    try:
        return ApiResponse(ok=True, data=run_strategy([b.model_dump() for b in request.bars], request.config))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/upload-csv")
def upload_csv(payload: CsvPayload) -> ApiResponse:
    """Run indicator trend signals on a CSV table file content of historical candle ticks."""
    try:
        df = pd.read_csv(StringIO(payload.content))
        bars = normalize_bars(df).to_dict(orient="records")
        return ApiResponse(ok=True, data=run_strategy(bars))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/bars/zebu")
def zebu_bars(request: ZebuBarsRequest) -> ApiResponse:
    """Fetch candles directly from active broker API historical server and run strategy compute."""
    try:
        client = get_active_client()
        bars = client.get_bars(
            exchange=request.exchange,
            symbol=request.symbol,
            interval=request.interval,
            start=request.start,
            end=request.end,
        )
        return ApiResponse(ok=True, data={"bars": bars, "strategy": run_strategy(bars)})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ==============================================================================
# SECTION 5: INTRADAY AUTOMATION WORKER CONTROL API
# ==============================================================================

@app.post("/api/intraday/start")
def intraday_start(request: IntradayRunRequest) -> ApiResponse:
    """Activate intraday automation worker, starting fetching and order-watching threads."""
    try:
        client = get_active_client()
        if request.live_trade and not client.settings.live_trading_enabled:
            raise HTTPException(status_code=400, detail="Real Trade mode is blocked. Enable LIVE_TRADING_ENABLED in config.")
        return ApiResponse(ok=True, data=runner.start(request))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/intraday/stop")
def intraday_stop() -> ApiResponse:
    """Deactivate intraday automation worker threads cleanly."""
    return ApiResponse(ok=True, data=runner.stop())


@app.post("/api/intraday/exit-open")
def intraday_exit_open() -> ApiResponse:
    """Immediately exit any active open order tracking inside the runner database list."""
    try:
        return ApiResponse(ok=True, data=runner.exit_open_order())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/intraday/status")
def intraday_status() -> ApiResponse:
    """Retrieve full metrics state and status variables for the active runner."""
    return ApiResponse(ok=True, data=runner.status())


@app.get("/api/intraday/orders")
def intraday_orders() -> ApiResponse:
    """Query high-frequency orders-only list updates (for table renders)."""
    return ApiResponse(ok=True, data=runner.orders_status())


@app.post("/api/intraday/order-adjust")
def intraday_order_adjust(request: OrderAdjustRequest) -> ApiResponse:
    """Manually overwrite target, SL, or trailing parameters on an active local order row."""
    try:
        return ApiResponse(ok=True, data=runner.adjust_order(request))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/intraday/manual-trade")
def intraday_manual_trade(request: ManualTradeActionRequest) -> ApiResponse:
    """Submit a manual BUY or SELL adjustment order on the active trade position."""
    try:
        return ApiResponse(ok=True, data=runner.manual_trade_action(
            order_key=request.order_key,
            action=request.action,
            quantity=request.quantity,
            price_str=request.price
        ))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/intraday/cancel-manual-trade")
def intraday_cancel_manual_trade(request: ManualTradeCancelRequest) -> ApiResponse:
    """Cancel any open pending manual limit orders on the active trade position."""
    try:
        return ApiResponse(ok=True, data=runner.cancel_manual_trade_action(
            order_key=request.order_key
        ))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ==============================================================================
# SECTION 6: MANUAL TRANSACTION PLACEMENT ROUTE
# ==============================================================================

@app.post("/api/order")
def order(request: OrderRequest) -> ApiResponse:
    """Submit a raw manual transaction order directly to the active broker (outside automation)."""
    try:
        client = get_active_client()
        data = client.place_order(**request.model_dump())
        return ApiResponse(ok=True, data=data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/debug/order-book")
def debug_order_book() -> ApiResponse:
    """Retrieve raw order book from active broker connection."""
    try:
        client = get_active_client()
        return ApiResponse(ok=True, data=client.get_order_book())
    except Exception as exc:
        return ApiResponse(ok=False, error=str(exc))


@app.get("/api/debug/order-history/{order_id}")
def debug_order_history(order_id: str) -> ApiResponse:
    """Retrieve raw order history from active broker connection."""
    try:
        client = get_active_client()
        return ApiResponse(ok=True, data=client.single_order_history(order_id))
    except Exception as exc:
        return ApiResponse(ok=False, error=str(exc))


@app.on_event("startup")
def startup_event():
    # Start persistent Upstox OAuth callback server on port 8090
    try:
        from brokers.upstox.client import start_persistent_callback_server
        start_persistent_callback_server()
    except Exception as exc:
        print(f"[STARTUP WARNING] Failed to initialize Upstox background server: {exc}")
