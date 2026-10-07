from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class StrategyConfig(BaseModel):
    ss_length: int = 10
    ss_target: int = -1
    ss_target1_mult: int = 4
    ss_target2_mult: int = 8
    ss_target3_mult: int = 12
    vol_length: int = 20
    vol_multiplier: float = 1.5
    use_adx_filter: bool = True
    adx_length: int = 14
    adx_threshold: float = 25
    entry_lookback: int = 3
    st_atr_len: int = 10
    st_factor: float = 3.0
    ib_mins: int = 60
    orb_mins: int = 15
    strike_interval: int = 50
    delta_proxy: float = 0.5
    tsl_tp3_to_tp2: bool = True
    tsl_points_to_tp3: float = 10
    tsl_1to1_increment: bool = True


class Bar(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


class Signal(BaseModel):
    time: datetime
    side: Literal["BUY", "SELL"]
    option_type: Literal["CE", "PE"]
    strike: int
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    tsl: float
    status: str
    pnl: float = 0


class BacktestRequest(BaseModel):
    symbol: str = "NIFTY"
    bars: list[Bar]
    config: StrategyConfig = Field(default_factory=StrategyConfig)


class CsvPayload(BaseModel):
    content: str


class ZebuBarsRequest(BaseModel):
    exchange: str = "NSE"
    symbol: str
    interval: int = 5
    start: datetime | None = None
    end: datetime | None = None


class IntradayRunRequest(BaseModel):
    exchange: str = "NSE"
    underlying: str = "NIFTY"
    symbol: Literal["Spot", "Future", "Option"] = "Option"
    timeframe_minutes: int = Field(default=3, ge=1)
    qty: int = Field(default=1, ge=1)
    strategy_type: str = "2"
    option_strategy: Literal["BUY", "SELL"] = "BUY"
    option_moneyness: int = 0
    option_expiry: str = "CURRENT_WEEK"
    option_qty_lots: int = Field(default=1, ge=1)
    candle_type: Literal["ohlc"] = "ohlc"
    start_time: str = "09:15:00"
    exit_time: str = "15:15:00"
    positional_trade: bool = False
    live_trade: bool = False
    reset_saved_positions: bool = True
    trading_mode: Literal["LONG", "SHORT", "LONG_SHORT"] = "LONG_SHORT"
    max_profit: float = 10000
    max_loss: float = -10000
    max_trades_per_day: int = Field(default=11, ge=1)
    order_product_type: Literal["NRML", "MIS"] = "MIS"
    sltp_instrument: Literal["option", "spot", "strategy"] = "option"
    sl_type: Literal["percentage", "points"] = "points"
    target_type: Literal["percentage", "points"] = "points"
    stoploss_value: float = 20
    target_value: float = 150
    trailing_stoploss: bool = True
    trail_start_type: Literal["percentage", "points"] = "points"
    trail_start_value: float = 0.0
    trail_when_moves_by: float = 1
    trail_move_sl_by: float = 1
    move_sl_to_cost: bool = True
    move_sl_to_cost_points: float = 10.0
    entry_order_mode: Literal["Aggressive_Entry", "True_Limit_LTP", "Limit_Below", "Limit_Above"] = "Aggressive_Entry"
    entry_limit_price: float | None = None
    exit_order_mode: Literal["False", "Aggressive_Exit", "True_Limit_LTP", "Limit_Below", "Limit_Above"] = "Aggressive_Exit"
    exit_limit_price: float | None = None
    enable_price_chasing: bool = False
    chase_max_retries: int = Field(default=5, ge=1)
    chase_timeout_seconds: int = Field(default=20, ge=1)
    chase_slippage_pct: float = Field(default=2.0, ge=0.0)
    chase_sweep_market: bool = True
    strike_interval: int = Field(default=50, ge=1)
    tick_size: float | None = None
    poll_seconds: int = Field(default=1, ge=1, le=3600)
    order_watch_seconds: float = Field(default=0.5, ge=0.2, le=5)
    lookback_days: int = Field(default=15, ge=1, le=60)
    paper_trade: bool = True
    config: StrategyConfig = Field(default_factory=StrategyConfig)


class OrderRequest(BaseModel):
    exchange: str = "NFO"
    tradingsymbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    product_type: str = "M"
    price_type: str = "LMT"
    price: float = 0
    trigger_price: float | None = None
    confirm_live: bool = False


class OrderAdjustRequest(BaseModel):
    order_key: str
    stoploss: float | None = None
    target: float | None = None
    trailing_stoploss: bool | None = None
    trail_start_value: float | None = None
    trail_when_moves_by: float | None = None
    trail_move_sl_by: float | None = None
    move_sl_to_cost: bool | None = None
    move_sl_to_cost_points: float | None = None


class ManualTradeActionRequest(BaseModel):
    order_key: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(..., ge=1)
    price: str = "At Mkt"


class ManualTradeCancelRequest(BaseModel):
    order_key: str



class ApiResponse(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None
