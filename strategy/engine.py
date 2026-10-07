from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import adx, atr, crossed_over, crossed_under, supertrend, vwap
from models.schemas import Signal, StrategyConfig

# ==============================================================================
# SECTION 1: CONSTANTS & TYPE DEFINITIONS
# ==============================================================================

DIR_NONE = 0
DIR_BUY = 1
DIR_SELL = -1


@dataclass
class TradeState:
    """Class to track the lifecycle status of a paper trade position."""
    signal: Signal
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    sl_hit: bool = False
    closed: bool = False


# ==============================================================================
# SECTION 2: DATA PREPROCESSING & NORMALIZATION
# ==============================================================================

def normalize_bars(bars: list[dict] | pd.DataFrame) -> pd.DataFrame:
    """Format, clean, and validate raw candle data columns into a standard DataFrame."""
    df = pd.DataFrame(bars).copy()
    if len(df) == 0:
        raise ValueError("No historical candles available. Check connection, symbol, or trading segment.")
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing OHLC columns: {', '.join(sorted(missing))}")
    if "volume" not in df.columns:
        df["volume"] = 0
        
    df["time"] = pd.to_datetime(df["time"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        
    df = df.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time").reset_index(drop=True)
    return df


# ==============================================================================
# SECTION 3: OPTION STRIKE & STATUS HELPERS
# ==============================================================================

def _atm_strike(close: float, interval: int) -> int:
    """Round the spot close price to the nearest option strike interval (ATM strike)."""
    return int(round(close / interval) * interval)


def _status(state: TradeState) -> str:
    """Decode trade position markers into user-friendly status labels."""
    if state.sl_hit:
        return "SL"
    if state.tp3_hit:
        return "TP3"
    if state.tp2_hit:
        return "TP2"
    if state.tp1_hit:
        return "TP1"
    return "OPEN"


# ==============================================================================
# SECTION 4: POSITION MONITORING & TSL UPDATER
# ==============================================================================

def _update_trade(state: TradeState, row: pd.Series, cfg: StrategyConfig, opposite: bool) -> None:
    """Monitor target price triggers and update Trailing Stop-Loss thresholds for open positions."""
    sig = state.signal
    is_ce = sig.option_type == "CE"

    # 1. Evaluate immediate price touch triggers
    sl_now = row.low <= sig.stop_loss if is_ce else row.high >= sig.stop_loss
    tp1_now = row.high >= sig.tp1 if is_ce else row.low <= sig.tp1
    tp2_now = row.high >= sig.tp2 if is_ce else row.low <= sig.tp2
    tp3_now = row.high >= sig.tp3 if is_ce else row.low <= sig.tp3

    state.sl_hit = state.sl_hit or sl_now
    state.tp1_hit = state.tp1_hit or tp1_now
    state.tp2_hit = state.tp2_hit or tp2_now
    state.tp3_hit = state.tp3_hit or tp3_now

    # 2. Adjust Trailing Stop-Loss (TSL) steps
    if state.tp1_hit and sig.tsl == sig.stop_loss:
        sig.tsl = sig.entry
    if state.tp2_hit and ((is_ce and sig.tsl < sig.tp1) or (not is_ce and sig.tsl > sig.tp1)):
        sig.tsl = sig.tp1
    if state.tp3_hit and cfg.tsl_tp3_to_tp2:
        sig.tsl = sig.tp2

    # 3. Dynamic points extension beyond Target 3
    points_beyond = row.close - sig.tp3 if is_ce else sig.tp3 - row.close
    if points_beyond >= cfg.tsl_points_to_tp3:
        sig.tsl = sig.tp3
    if cfg.tsl_1to1_increment and points_beyond > cfg.tsl_points_to_tp3:
        trail = sig.tp3 + (points_beyond - cfg.tsl_points_to_tp3) * (1 if is_ce else -1)
        sig.tsl = max(sig.tsl, trail) if is_ce else min(sig.tsl, trail)

    # 4. Compile closure state and status labels
    tsl_hit = row.low <= sig.tsl if is_ce else row.high >= sig.tsl
    should_exit = sl_now or tp3_now or tsl_hit or opposite

    # Calculate live PnL based on current close or specific trigger exit price
    if should_exit:
        if tsl_hit:
            exit_price = sig.tsl
        elif sl_now:
            exit_price = sig.stop_loss
        elif tp3_now:
            exit_price = sig.tp3
        else:
            exit_price = row.close
    else:
        exit_price = row.close

    live_pnl = ((exit_price - sig.entry) if is_ce else (sig.entry - exit_price)) * cfg.delta_proxy
    sig.pnl = float(live_pnl)
    sig.status = _status(state)
    
    if should_exit:
        state.closed = True
        if tsl_hit:
            sig.status = f"{sig.status}_TSL" if sig.status != "OPEN" else "TSL"
        elif opposite:
            sig.status = f"{sig.status}_OPP" if sig.status != "OPEN" else "OPPOSITE"
        elif sl_now:
            sig.status = "SL"
        elif tp3_now:
            sig.status = "TP3"


# ==============================================================================
# SECTION 5: STRATEGY ENGINE & SIGNAL COMPUTATION
# ==============================================================================

def run_strategy(bars: list[dict] | pd.DataFrame, cfg: StrategyConfig | None = None) -> dict:
    """Calculate technical indicators, compute trend signals, and trace trade lifecycles over historical candles."""
    cfg = cfg or StrategyConfig()
    df = normalize_bars(bars)
    
    if len(df) == 0:
        raise ValueError("No historical candles available. Check connection, symbol, or trading segment.")
    
    # Auto-pad the dataframe by prepending dummy flat candles if there are fewer than 450 candles.
    # The strategy calculates ATR(200) smoothed by SMA(200), which requires a combined 400 periods
    # of warmup data to yield non-NaN indicator values for active trade signals.
    required = max(450, cfg.ss_length * 2)
    if len(df) < required:
        pad_count = required - len(df) + 10
        first_row = df.iloc[0]
        t0 = first_row["time"]
        if isinstance(t0, str):
            t0 = pd.to_datetime(t0)
            
        # Estimate timeframe interval delta
        if len(df) > 1:
            t1 = df.iloc[1]["time"]
            if isinstance(t1, str):
                t1 = pd.to_datetime(t1)
            interval_delta = t1 - t0
        else:
            interval_delta = pd.Timedelta(minutes=5)
            
        pad_rows = []
        for i in range(pad_count, 0, -1):
            pad_time = t0 - (interval_delta * i)
            pad_rows.append({
                "time": pad_time,
                "open": float(first_row["open"]),
                "high": float(first_row["high"]),
                "low": float(first_row["low"]),
                "close": float(first_row["close"]),
                "volume": 0.0,
            })
        pad_df = pd.DataFrame(pad_rows)
        # Ensure column types and structure align
        for col in df.columns:
            if col not in pad_df.columns:
                pad_df[col] = df[col].iloc[0]
        pad_df = pad_df[df.columns]
        df = pd.concat([pad_df, df], ignore_index=True)

    # 1. Calculate Technical Indicators (ATR, SMA, DMI/ADX, VWAP, Supertrend)
    df["atr_200"] = atr(df, 200)
    df["ss_atr_value"] = df["atr_200"].rolling(200, min_periods=200).mean() * 0.8
    df["sma_high"] = df["high"].rolling(cfg.ss_length, min_periods=cfg.ss_length).mean() + df["ss_atr_value"]
    df["sma_low"] = df["low"].rolling(cfg.ss_length, min_periods=cfg.ss_length).mean() - df["ss_atr_value"]
    df["avg_volume"] = df["volume"].rolling(cfg.vol_length, min_periods=cfg.vol_length).mean()
    df["high_volume"] = df["volume"] > df["avg_volume"] * cfg.vol_multiplier
    df["vwap"] = vwap(df)
    df = pd.concat([df, adx(df, cfg.adx_length), supertrend(df, cfg.st_atr_len, cfg.st_factor)], axis=1)
    
    df["signal"] = ""
    df["trend"] = False

    pending_dir = DIR_NONE
    pending_bar: int | None = None
    trend = False
    buy_count = 0
    sell_count = 0
    active: list[TradeState] = []
    all_signals: list[Signal] = []

    # 2. Iterate through candle bars to calculate trend breaks and entry triggers
    for i, row in df.iterrows():
        if np.isnan(row.sma_high) or np.isnan(row.sma_low):
            continue

        prev_trend = trend
        if crossed_over(df["close"], df["sma_high"], i):
            trend = True
        if crossed_under(df["close"], df["sma_low"], i):
            trend = False
        df.at[i, "trend"] = trend

        # Detect raw crossover signals
        raw_up = trend != prev_trend and not prev_trend
        raw_down = trend != prev_trend and prev_trend
        if raw_up:
            pending_dir = DIR_BUY
            pending_bar = i
        if raw_down:
            pending_dir = DIR_SELL
            pending_bar = i

        signal_up = False
        signal_down = False
        
        # Verify signal entry lookback and ADX filter criteria
        if pending_dir != DIR_NONE and pending_bar is not None:
            candles_since = i - pending_bar
            if candles_since < cfg.entry_lookback:
                direction_valid = trend if pending_dir == DIR_BUY else not trend
                adx_ok = (not cfg.use_adx_filter) or row.adx > cfg.adx_threshold
                if direction_valid and adx_ok:
                    signal_up = pending_dir == DIR_BUY
                    signal_down = pending_dir == DIR_SELL
                    pending_dir = DIR_NONE
                    pending_bar = None
            elif candles_since >= cfg.entry_lookback:
                pending_dir = DIR_NONE
                pending_bar = None

        opposite_for_ce = signal_down
        opposite_for_pe = signal_up
        
        # 3. Update existing active trade states on this bar
        for state in active:
            if not state.closed:
                _update_trade(
                    state, 
                    row, 
                    cfg, 
                    opposite_for_ce if state.signal.option_type == "CE" else opposite_for_pe
                )

        # 4. Trigger new signals and formulate stop/target boundaries
        if signal_up or signal_down:
            is_buy = signal_up
            buy_count += int(signal_up)
            sell_count += int(signal_down)
            
            base = row.sma_low if is_buy else row.sma_high
            atr_mult = row.ss_atr_value * (1 if is_buy else -1)
            entry = float(row.close)
            
            sig = Signal(
                time=row.time.to_pydatetime(),
                side="BUY" if is_buy else "SELL",
                option_type="CE" if is_buy else "PE",
                strike=_atm_strike(entry, cfg.strike_interval),
                entry=entry,
                stop_loss=float(base),
                tp1=float(entry + atr_mult * (cfg.ss_target1_mult + cfg.ss_target)),
                tp2=float(entry + atr_mult * (cfg.ss_target2_mult + cfg.ss_target * 2)),
                tp3=float(entry + atr_mult * (cfg.ss_target3_mult + cfg.ss_target * 3)),
                tsl=float(base),
                status="OPEN",
            )
            
            active.append(TradeState(sig))
            all_signals.append(sig)
            df.at[i, "signal"] = sig.side

    last = df.iloc[-1]
    
    return {
        "summary": {
            "bars": len(df),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "last_close": float(last.close),
            "last_adx": float(last.adx),
            "last_vwap": None if pd.isna(last.vwap) else float(last.vwap),
            "last_trend": "BULLISH" if bool(last.trend) else "BEARISH",
        },
        "signals": [s.model_dump(mode="json") for s in all_signals],
        "bars": df.replace({np.nan: None}).tail(500).to_dict(orient="records"),
    }
