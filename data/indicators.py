from __future__ import annotations

import pandas as pd
import ta.momentum
import ta.trend
import ta.volatility
import ta.volume

from config.settings import (
    ATR_PERIOD,
    BB_PERIOD,
    BB_STD,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    RSI_PERIOD,
)

_MIN_ROWS = 30


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Append technical indicator columns to a copy of `df` and return it."""
    if len(df) < _MIN_ROWS:
        raise ValueError(
            f"compute_indicators requires at least {_MIN_ROWS} rows; got {len(df)}"
        )

    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    df["rsi_14"] = ta.momentum.RSIIndicator(close, window=RSI_PERIOD).rsi()

    macd_ind = ta.trend.MACD(
        close, window_fast=MACD_FAST, window_slow=MACD_SLOW, window_sign=MACD_SIGNAL
    )
    df["macd"] = macd_ind.macd()
    df["macd_signal"] = macd_ind.macd_signal()
    df["macd_hist"] = macd_ind.macd_diff()

    bb_ind = ta.volatility.BollingerBands(close, window=BB_PERIOD, window_dev=BB_STD)
    df["bb_lower"] = bb_ind.bollinger_lband()
    df["bb_mid"] = bb_ind.bollinger_mavg()
    df["bb_upper"] = bb_ind.bollinger_hband()
    band_width = df["bb_upper"] - df["bb_lower"]
    df["bb_pct"] = (close - df["bb_lower"]) / band_width.replace(0, float("nan"))

    df["ema_9"] = ta.trend.EMAIndicator(close, window=9).ema_indicator()
    df["ema_21"] = ta.trend.EMAIndicator(close, window=21).ema_indicator()

    df["atr_14"] = ta.volatility.AverageTrueRange(
        high, low, close, window=ATR_PERIOD
    ).average_true_range()

    # Rolling 24h VWAP approximation (96 bars at 15m)
    typical_price = (high + low + close) / 3
    cum_vol = volume.rolling(window=96, min_periods=1).sum()
    cum_tp_vol = (typical_price * volume).rolling(window=96, min_periods=1).sum()
    df["vwap"] = cum_tp_vol / cum_vol.replace(0, float("nan"))

    vol_sma = ta.trend.SMAIndicator(volume, window=20).sma_indicator()
    df["volume_ratio"] = volume / vol_sma.replace(0, float("nan"))

    return df.dropna()


def get_signal_summary(df: pd.DataFrame) -> dict:
    if df.empty:
        raise ValueError("get_signal_summary requires a non-empty DataFrame")

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    def _f(key: str, decimals: int = 2) -> float:
        val = last.get(key, float("nan"))
        return round(float(val), decimals)

    price = _f("close", 8)
    rsi = _f("rsi_14")
    macd = _f("macd", 6)
    macd_sig = _f("macd_signal", 6)
    macd_hist = _f("macd_hist", 6)
    bb_upper = _f("bb_upper", 8)
    bb_mid = _f("bb_mid", 8)
    bb_lower = _f("bb_lower", 8)
    bb_pct = _f("bb_pct", 3)
    ema_9 = _f("ema_9", 8)
    ema_21 = _f("ema_21", 8)
    atr = _f("atr_14", 8)
    vwap = _f("vwap", 8)
    vol_ratio = _f("volume_ratio", 2)

    macd_bullish = macd > macd_sig
    ema_bullish = ema_9 > ema_21
    above_vwap = price > vwap if vwap == vwap else None
    prev_macd_hist = round(float(prev.get("macd_hist", float("nan"))), 6)
    macd_hist_rising = macd_hist > prev_macd_hist

    result = {
        "price": price,
        "rsi_14": rsi,
        "macd": macd,
        "macd_signal": macd_sig,
        "macd_hist": macd_hist,
        "macd_bullish": macd_bullish,
        "macd_hist_rising": macd_hist_rising,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "bb_pct": bb_pct,
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_bullish": ema_bullish,
        "atr_14": atr,
        "atr_pct_price": round(atr / price * 100, 3) if price else 0.0,
        "vwap": vwap,
        "above_vwap": above_vwap,
        "volume_ratio": vol_ratio,
    }

    result["summary_text"] = _build_summary(result)
    return result


def _build_summary(s: dict) -> str:
    price = s["price"]
    rsi = s["rsi_14"]
    bb_pct = s["bb_pct"]
    vol_ratio = s["volume_ratio"]
    atr_pct = s["atr_pct_price"]

    if rsi >= 70:
        rsi_label = "overbought"
    elif rsi <= 30:
        rsi_label = "oversold"
    else:
        rsi_label = "neutral"

    if bb_pct >= 0.8:
        bb_label = "near upper band (extended)"
    elif bb_pct <= 0.2:
        bb_label = "near lower band (compressed)"
    else:
        bb_label = "mid-band"

    macd_dir = "bullish crossover" if s["macd_bullish"] else "bearish crossover"
    macd_mom = "rising" if s["macd_hist_rising"] else "falling"
    ema_trend = (
        "uptrend (EMA9 > EMA21)" if s["ema_bullish"] else "downtrend (EMA9 < EMA21)"
    )
    vwap_pos = (
        "above rolling VWAP (bullish bias)"
        if s["above_vwap"]
        else "below rolling VWAP (bearish bias)"
        if s["above_vwap"] is not None
        else "VWAP unavailable"
    )
    vol_label = (
        f"{vol_ratio:.1f}x average ({'elevated' if vol_ratio > 1.2 else 'below-average' if vol_ratio < 0.8 else 'normal'})"
    )

    return (
        f"Price: {price}\n"
        f"RSI(14): {rsi:.1f} — {rsi_label}\n"
        f"MACD: {s['macd']:.6f} vs signal {s['macd_signal']:.6f} → {macd_dir}, histogram {macd_mom}\n"
        f"Bollinger Bands: price at {bb_pct:.1%} of band ({bb_label})\n"
        f"  Upper: {s['bb_upper']}  Mid: {s['bb_mid']}  Lower: {s['bb_lower']}\n"
        f"EMA trend: {ema_trend} (EMA9={s['ema_9']}, EMA21={s['ema_21']})\n"
        f"ATR(14): {s['atr_14']} ({atr_pct:.2f}% of price — "
        f"{'high' if atr_pct > 2.0 else 'low'} volatility)\n"
        f"Rolling 24h VWAP: {s['vwap']} — {vwap_pos}\n"
        f"Volume: {vol_label}\n"
        f"Market: 24/7 crypto — no session open/close"
    )
