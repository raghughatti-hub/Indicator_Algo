import numpy as np
import pandas as pd


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    values = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return values.max(axis=1)


def rma(series: pd.Series, length: int) -> pd.Series:
    if length <= 0:
        raise ValueError("Indicator length must be positive")
    result = pd.Series(np.nan, index=series.index, dtype=float)
    seed = []
    value = None
    for i, item in enumerate(series):
        if pd.isna(item):
            continue
        if value is None:
            seed.append(float(item))
            if len(seed) < length:
                continue
            value = sum(seed) / length
        else:
            value = (value * (length - 1) + float(item)) / length
        result.iat[i] = value
    return result


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    return rma(true_range(df), length)


def adx(df: pd.DataFrame, length: int) -> pd.DataFrame:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr_rma = rma(true_range(df), length)
    plus_di = 100 * rma(pd.Series(plus_dm, index=df.index), length) / tr_rma
    minus_di = 100 * rma(pd.Series(minus_dm, index=df.index), length) / tr_rma
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame(
        {
            "dmi_plus": plus_di.fillna(0),
            "dmi_minus": minus_di.fillna(0),
            "adx": rma(dx, length).fillna(0),
        },
        index=df.index,
    )


def vwap(df: pd.DataFrame) -> pd.Series:
    day = df["time"].dt.date
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    return pv.groupby(day).cumsum() / df["volume"].replace(0, np.nan).groupby(day).cumsum()


def supertrend(df: pd.DataFrame, atr_len: int, factor: float) -> pd.DataFrame:
    atr_val = atr(df, atr_len)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + factor * atr_val
    lower = hl2 - factor * atr_val
    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = pd.Series(1, index=df.index, dtype=float)
    trend = pd.Series(np.nan, index=df.index, dtype=float)

    for i in range(len(df)):
        if pd.isna(atr_val.iat[i]):
            continue
        prev = i - 1
        if i == 0 or pd.isna(trend.iat[prev]):
            final_upper.iat[i] = upper.iat[i]
            final_lower.iat[i] = lower.iat[i]
            trend.iat[i] = lower.iat[i]
            continue
        final_upper.iat[i] = (
            upper.iat[i]
            if upper.iat[i] < final_upper.iat[prev] or df["close"].iat[prev] > final_upper.iat[prev]
            else final_upper.iat[prev]
        )
        final_lower.iat[i] = (
            lower.iat[i]
            if lower.iat[i] > final_lower.iat[prev] or df["close"].iat[prev] < final_lower.iat[prev]
            else final_lower.iat[prev]
        )
        if direction.iat[prev] == -1 and df["close"].iat[i] > final_upper.iat[prev]:
            direction.iat[i] = 1
        elif direction.iat[prev] == 1 and df["close"].iat[i] < final_lower.iat[prev]:
            direction.iat[i] = -1
        else:
            direction.iat[i] = direction.iat[prev]
        trend.iat[i] = final_lower.iat[i] if direction.iat[i] == 1 else final_upper.iat[i]

    return pd.DataFrame({"supertrend": trend, "st_direction": direction}, index=df.index)


def crossed_over(left: pd.Series, right: pd.Series, i: int) -> bool:
    if i == 0:
        return False
    return left.iat[i - 1] <= right.iat[i - 1] and left.iat[i] > right.iat[i]


def crossed_under(left: pd.Series, right: pd.Series, i: int) -> bool:
    if i == 0:
        return False
    return left.iat[i - 1] >= right.iat[i - 1] and left.iat[i] < right.iat[i]
