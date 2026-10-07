from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict, model_validator


class TradingModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class StrategyConfig(TradingModel):
    ss_length: int = Field(default=10, ge=1, le=10000)
    ss_target: int = -1
    ss_target1_mult: int = 4
    ss_target2_mult: int = 8
    ss_target3_mult: int = 12
    vol_length: int = Field(default=20, ge=1, le=10000)
    vol_multiplier: float = Field(default=1.5, gt=0)
    use_supertrend_filter: bool = False
    use_adx_filter: bool = True
    adx_length: int = Field(default=14, ge=1, le=10000)
    adx_threshold: float = 25
    entry_lookback: int = Field(default=3, ge=1, le=10000)
    st_atr_len: int = Field(default=10, ge=1, le=10000)
    st_factor: float = Field(default=3.0, gt=0)
    ib_mins: int = Field(default=60, ge=1, le=10000)
    orb_mins: int = Field(default=15, ge=1, le=10000)
    strike_interval: int = Field(default=50, ge=1, le=10000)
    delta_proxy: float = Field(default=0.5, gt=0)
    tsl_tp3_to_tp2: bool = True
    tsl_points_to_tp3: float = Field(default=10, gt=0)
    tsl_1to1_increment: bool = True

    @model_validator(mode="after")
    def validate_targets(self):
        targets = (self.ss_target1_mult + self.ss_target,
                   self.ss_target2_mult + 2*self.ss_target,
                   self.ss_target3_mult + 3*self.ss_target)
        if not 0 < targets[0] < targets[1] < targets[2]:
            raise ValueError("Targets must be positive and strictly increasing")
        return self


class Bar(TradingModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


class Signal(TradingModel):
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
    closed: bool = False
    exit_reason: str | None = None
    exit_time: datetime | None = None


class BacktestRequest(TradingModel):
    symbol: str = "NIFTY"
    bars: list[Bar]
    config: StrategyConfig = Field(default_factory=StrategyConfig)


class CsvPayload(TradingModel):
    content: str


class ZebuBarsRequest(TradingModel):
    exchange: str = "NSE"
    symbol: str
    interval: int = Field(default=5, ge=1, le=1440)
    start: datetime | None = None
    end: datetime | None = None


class IntradayRunRequest(TradingModel):
    exchange: str = "NSE"
    underlying: str = "NIFTY"
    symbol: Literal["Spot", "Future", "Option"] = "Option"
    timeframe_minutes: int = Field(default=3, ge=1, le=1440)
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
    max_profit: float = Field(default=10000, gt=0)
    max_loss: float = Field(default=-10000, lt=0)
    max_trades_per_day: int = Field(default=11, ge=1)
    order_product_type: Literal["NRML", "MIS"] = "MIS"
    sltp_instrument: Literal["option", "spot", "strategy"] = "option"
    sl_type: Literal["percentage", "points"] = "points"
    target_type: Literal["percentage", "points"] = "points"
    stoploss_value: float = Field(default=20, gt=0)
    target_value: float = Field(default=150, gt=0)
    trailing_stoploss: bool = True
    trail_start_type: Literal["percentage", "points"] = "points"
    trail_start_value: float = Field(default=0.0, ge=0)
    trail_when_moves_by: float = Field(default=1, gt=0)
    trail_move_sl_by: float = Field(default=1, gt=0)
    move_sl_to_cost: bool = True
    move_sl_to_cost_points: float = Field(default=10.0, ge=0)
    entry_order_mode: Literal["Aggressive_Entry", "True_Limit_LTP", "Limit_Below", "Limit_Above"] = "Aggressive_Entry"
    entry_limit_price: float | None = Field(default=None, gt=0)
    exit_order_mode: Literal["False", "Aggressive_Exit", "True_Limit_LTP", "Limit_Below", "Limit_Above"] = "Aggressive_Exit"
    exit_limit_price: float | None = Field(default=None, gt=0)
    continuous_execution: bool = True
    entry_slippage_pct: float = Field(default=0.3, ge=0, le=10)
    exit_slippage_pct: float = Field(default=0.3, ge=0, le=10)
    protective_exit_slippage_pct: float = Field(default=0.5, ge=0, le=10)
    entry_slippage_points: float | None = Field(default=None, ge=0)
    exit_slippage_points: float | None = Field(default=None, ge=0)
    protective_exit_slippage_points: float | None = Field(default=None, ge=0)
    entry_timeout_seconds: float = Field(default=10, gt=0, le=300)
    execution_reprice_seconds: float = Field(default=1, ge=0.5, le=30)
    execution_max_attempts: int = Field(default=10, ge=1, le=100)
    max_entry_spread_pct: float = Field(default=1, ge=0, le=20)
    enable_price_chasing: bool = False
    chase_max_retries: int = Field(default=5, ge=1)
    chase_timeout_seconds: int = Field(default=20, ge=1)
    chase_slippage_pct: float = Field(default=2.0, ge=0.0)
    chase_sweep_market: bool = Field(default=True, deprecated=True, description="Legacy final LIMIT attempt; never a market order")
    strike_interval: int = Field(default=50, ge=1)
    tick_size: float | None = Field(default=None, gt=0)
    poll_seconds: int = Field(default=1, ge=1, le=3600)
    order_watch_seconds: float = Field(default=0.5, ge=0.2, le=5)
    lookback_days: int = Field(default=15, ge=1, le=60)
    paper_trade: bool = True
    config: StrategyConfig = Field(default_factory=StrategyConfig)

    @model_validator(mode="after")
    def validate_session(self):
        from datetime import time
        try:
            start, end = time.fromisoformat(self.start_time), time.fromisoformat(self.exit_time)
        except ValueError as exc:
            raise ValueError("Use HH:MM[:SS] for session times") from exc
        if start >= end:
            raise ValueError("Start time must precede exit time")
        if self.exit_order_mode == "False" and self.live_trade:
            raise ValueError("Live trading requires an enabled exit mode")
        if self.sl_type == "percentage" and self.stoploss_value >= 100:
            raise ValueError("Percentage stop distance must be below 100")
        if self.target_type == "percentage" and self.option_strategy == "SELL" and self.target_value >= 100:
            raise ValueError("Short target distance must be below 100 percent")
        return self


class OrderRequest(TradingModel):
    exchange: str = "NFO"
    tradingsymbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(..., ge=1)
    product_type: str = "M"
    price_type: Literal["LMT"] = "LMT"
    price: float = 0
    trigger_price: float | None = None
    confirm_live: bool = False


class OrderAdjustRequest(TradingModel):
    order_key: str
    stoploss: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    trailing_stoploss: bool | None = None
    trail_start_value: float | None = Field(default=None, ge=0)
    trail_when_moves_by: float | None = Field(default=None, gt=0)
    trail_move_sl_by: float | None = Field(default=None, gt=0)
    move_sl_to_cost: bool | None = None
    move_sl_to_cost_points: float | None = Field(default=None, ge=0)


class ManualTradeActionRequest(TradingModel):
    order_key: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(..., ge=1)
    price: str = "At Mkt"


class ManualTradeCancelRequest(TradingModel):
    order_key: str



class ApiResponse(TradingModel):
    ok: bool
    data: Any = None
    error: str | None = None
