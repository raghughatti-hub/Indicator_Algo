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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
from utils.security import authorized, local_browser, dashboard_access_allowed, control_token, runtime_dir
from html import escape

# ==============================================================================
# SECTION 1: IMPORTS & API INITIALIZATION
# ==============================================================================

app = FastAPI(
    title="NSE Tools Python Algo - Multi-Broker Refactored Edition",
    version="0.2.0",
    description="Local strategy and broker execution dashboard."
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
        runner.attach_broker(broker_client)
    return broker_client


# Instantiate runner — broker client is initialized lazily on first request
runner = IntradayRunner(None,state_path=runtime_dir()/"trading_state.json")

# Mount local static file handler for index, styles, and script
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ==============================================================================
# SECTION 2: FRONTEND INDEX ROUTE
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    if not dashboard_access_allowed(request):
        raise HTTPException(status_code=403,detail="Cross-origin control access denied")
    response=HTMLResponse((BASE_DIR/"static"/"index.html").read_text(encoding="utf-8"))
    if local_browser(request):
        response.set_cookie("algo_session",control_token(),httponly=True,samesite="strict",secure=request.url.scheme=="https")
    return response


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

    if runner.has_unsettled_orders():
        candidate = get_active_client()
        if broker_name != candidate.settings.active_broker or candidate.connected:
            raise HTTPException(status_code=409,detail="Settle open/pending trades before changing broker session")
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

    runner.attach_broker(broker_client)

    # Connect client
    ok = broker_client.connect_with_token()
    if not ok:
        ok = broker_client.connect_auto_oauth()
    if ok:
        runner.attach_broker(broker_client)
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
    if runner.has_unsettled_orders():
        raise HTTPException(status_code=409,detail="Cannot disconnect with open/pending trades")
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
def upstox_callback(code: str = "", state: str = "", error: str = ""):
    client=get_active_client()
    if error:
        return HTMLResponse("<h2>Login failed</h2><p>"+escape(error)+"</p>",status_code=400)
    if client.settings.active_broker != "upstox" or not code:
        raise HTTPException(status_code=400,detail="No pending Upstox authorization")
    ok=client.connect_oauth_code(code,state=state)
    if not ok:
        return HTMLResponse("<h2>Login failed</h2><p>"+escape(client.last_error or "Authorization failed")+"</p>",status_code=400)
    runner.attach_broker(client)
    return HTMLResponse('<h2>Login successful</h2><a href="/">Return to terminal</a>')


@app.get("/api/status")
def status() -> ApiResponse:
    """Retrieve current Broker Client connection status and credential state."""
    client = get_active_client()
    return ApiResponse(ok=True, data=client.status())


@app.post("/api/settings/reload")
def settings_reload() -> ApiResponse:
    """Reload environment variables from .env and clear cached credentials settings."""
    global broker_client
    if runner.has_unsettled_orders():
        raise HTTPException(status_code=409,detail="Cannot reload broker configuration with unsettled trades")
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
    return connect_broker(ConnectRequest(broker=get_settings().active_broker))


class OAuthCodeRequest(BaseModel):
    code: str
    state: str


@app.post("/api/broker/oauth-code")
def broker_oauth(request: OAuthCodeRequest):
    client=get_active_client()
    if runner.has_unsettled_orders() and client.connected:
        raise HTTPException(status_code=409,detail="Do not replace an active session while trading")
    ok=client.connect_oauth_code(request.code,state=request.state)
    if ok:
        runner.attach_broker(client)
    return ApiResponse(ok=ok,data=client.status(),error=None if ok else client.last_error)


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


@app.post("/api/intraday/reconcile")
def reconcile_submissions():
    try:
        return ApiResponse(ok=True,data=runner.reconcile_submissions())
    except Exception as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc


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
    if request.confirm_live:
        raise HTTPException(status_code=409,detail="Use tracked runner entry/manual controls for live orders")
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
    # A single owner prevents two processes from placing orders from the same state.
    from utils.security import acquire_engine_lock
    app.state.engine_lock = acquire_engine_lock()
    # Loopback redirect bridge; the main callback owns state and token exchange.
    try:
        from brokers.upstox.client import start_persistent_callback_server
        start_persistent_callback_server()
    except Exception as exc:
        print(f"[STARTUP WARNING] Failed to initialize Upstox background server: {exc}")


@app.middleware("http")
async def protect_control_api(request: Request, call_next):
    if request.url.path.startswith("/api/") and not authorized(request):
        return JSONResponse({"detail":"Control authentication required"},status_code=401)
    response=await call_next(request)
    response.headers["Cache-Control"]="no-store"
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
    return response


# Authorization codes must not appear in access logs.
import logging
class RedactOAuthAccessLogs(logging.Filter):
    def filter(self, record):
        if isinstance(record.args,tuple):
            record.args=tuple(arg.split("?",1)[0]+"?<redacted>" if isinstance(arg,str) and "code=" in arg else arg for arg in record.args)
        return True
logging.getLogger("uvicorn.access").addFilter(RedactOAuthAccessLogs())

@app.on_event("shutdown")
def shutdown_event():
    runner.stop()
    handle = getattr(app.state, "engine_lock", None)
    if handle:
        handle.close()
