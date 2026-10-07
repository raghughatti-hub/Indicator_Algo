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
        
    from utils.clock import market_time
    df["time"] = pd.to_datetime([market_time(t) for t in df["time"]])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        
    values = df[["open", "high", "low", "close", "volume"]]
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError("OHLCV values must be finite numbers")
    if (df["low"] > df[["open", "close"]].min(axis=1)).any() or (df["high"] < df[["open", "close"]].max(axis=1)).any() or (df["volume"] < 0).any():
        raise ValueError("Invalid OHLC range or negative volume")
    df = df.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
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
    """Conservative OHLC execution: the pre-bar stop wins ambiguous candles.

    A trailing level calculated from this bar is effective on the next bar.
    """
    sig = state.signal
    direction = 1 if sig.option_type == "CE" else -1
    old_stop = max(sig.stop_loss, sig.tsl) if direction == 1 else min(sig.stop_loss, sig.tsl)
    stop_hit = row.low <= old_stop if direction == 1 else row.high >= old_stop
    target_hit = row.high >= sig.tp3 if direction == 1 else row.low <= sig.tp3
    exit_price = float(row.close)
    reason = None
    if stop_hit:
        opening = float(row.get("open", old_stop))
        exit_price = min(opening, old_stop) if direction == 1 else max(opening, old_stop)
        reason = "TSL" if old_stop != sig.stop_loss else "SL"
        state.sl_hit = reason == "SL"
    elif target_hit:
        exit_price = sig.tp3
        reason = "TP3"
        state.tp3_hit = True
    elif opposite:
        reason = "OPPOSITE"

    if reason:
        state.closed = True
        sig.closed = True
        sig.exit_reason = reason
        sig.exit_time = row.time.to_pydatetime() if hasattr(row.time, "to_pydatetime") else row.time
        sig.status = reason
    else:
        state.tp1_hit |= bool(row.high >= sig.tp1 if direction == 1 else row.low <= sig.tp1)
        state.tp2_hit |= bool(row.high >= sig.tp2 if direction == 1 else row.low <= sig.tp2)
        next_stop = sig.tsl
        if state.tp1_hit:
            next_stop = max(next_stop, sig.entry) if direction == 1 else min(next_stop, sig.entry)
        if state.tp2_hit:
            next_stop = max(next_stop, sig.tp1) if direction == 1 else min(next_stop, sig.tp1)
        # TP3 exits the trade, so extension beyond TP3 does not alter this lifecycle.
        sig.tsl = next_stop
        sig.status = _status(state)
    sig.pnl = float((exit_price - sig.entry) * direction * cfg.delta_proxy)


# ==============================================================================
# SECTION 5: STRATEGY ENGINE & SIGNAL COMPUTATION
# ==============================================================================

def run_strategy(bars: list[dict] | pd.DataFrame, cfg: StrategyConfig | None = None) -> dict:
    """Calculate technical indicators, compute trend signals, and trace trade lifecycles over historical candles."""
    cfg = cfg or StrategyConfig()
    df = normalize_bars(bars)
    
    if len(df) == 0:
        raise ValueError("No historical candles available. Check connection, symbol, or trading segment.")
    
    required = max(399, cfg.ss_length, cfg.vol_length, 2 * cfg.adx_length if cfg.use_adx_filter else 1, cfg.st_atr_len if cfg.use_supertrend_filter else 1)

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
                st_ok = (not cfg.use_supertrend_filter) or (not pd.isna(row.supertrend) and (row.st_direction == (1 if pending_dir == DIR_BUY else -1)))
                if direction_valid and adx_ok and st_ok:
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
            "warmup_required": required,
            "warmup_complete": len(df) >= required,
            "pnl_basis": "underlying points times delta_proxy; excludes fills, costs and option pricing",
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
