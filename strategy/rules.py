from __future__ import annotations

from typing import Optional

import pandas as pd

from config.settings import MAX_OPEN_POSITIONS, MIN_24H_VOLUME_USD
from logs.logger import get_logger

logger = get_logger(__name__)

_MIN_PRICE = 0.0001
_MIN_VOLUME_RATIO = 0.5
_MAX_ATR_PCT = 0.08
_MAX_SPREAD_PCT_MAJOR = 0.001
_MAX_SPREAD_PCT_ALT = 0.003
_MIN_CONFIDENCE = 0.65
_EXHAUSTION_BARS = 5

_MAJORS = {"BTC/USDT", "ETH/USDT", "BNB/USDT"}


class RuleFilter:
    """Hard rule-based gate that runs before (and after) the AI signal."""

    def pre_filter(
        self,
        symbol: str,
        df: pd.DataFrame,
        indicators: dict,
    ) -> tuple[bool, str]:
        price = float(indicators.get("price", 0))
        if price < _MIN_PRICE:
            return self._block(symbol, f"price ${price:.8f} below minimum")

        vol_ratio = indicators.get("volume_ratio")
        if vol_ratio is not None and _is_float(vol_ratio):
            if float(vol_ratio) < _MIN_VOLUME_RATIO:
                return self._block(
                    symbol,
                    f"volume ratio {vol_ratio:.2f} < {_MIN_VOLUME_RATIO} (illiquid)",
                )

        quote_vol = indicators.get("quote_volume_24h")
        if quote_vol is not None and _is_float(quote_vol):
            if float(quote_vol) < MIN_24H_VOLUME_USD:
                return self._block(
                    symbol,
                    f"24h volume ${float(quote_vol):,.0f} < ${MIN_24H_VOLUME_USD:,.0f}",
                )

        atr_pct = indicators.get("atr_pct_price")
        if atr_pct is not None and _is_float(atr_pct):
            if float(atr_pct) / 100 > _MAX_ATR_PCT:
                return self._block(
                    symbol,
                    f"ATR {atr_pct:.2f}% exceeds {_MAX_ATR_PCT*100:.1f}% limit",
                )

        spread = indicators.get("spread")
        mid = indicators.get("mid_price") or price
        if spread is not None and _is_float(spread) and mid > 0:
            spread_pct = float(spread) / float(mid)
            max_spread = (
                _MAX_SPREAD_PCT_MAJOR if symbol in _MAJORS else _MAX_SPREAD_PCT_ALT
            )
            if spread_pct > max_spread:
                return self._block(
                    symbol,
                    f"spread {spread_pct*100:.3f}% > {max_spread*100:.2f}% limit",
                )

        exhausted, exhaust_reason = _check_exhaustion(df, symbol)
        if exhausted:
            return self._block(symbol, exhaust_reason)

        logger.debug(f"[{symbol}] pre_filter: passed all checks")
        return True, "ok"

    def post_filter(
        self,
        signal: dict,
        portfolio: dict,
    ) -> tuple[bool, str]:
        symbol = signal.get("symbol", "UNKNOWN")
        sig = signal.get("signal", "HOLD")
        confidence = float(signal.get("confidence", 0.0))
        held: set[str] = portfolio.get("held_symbols", set())
        open_count: int = int(portfolio.get("open_positions", 0))

        if symbol in held:
            return self._block(symbol, "already holding this pair")

        if open_count >= MAX_OPEN_POSITIONS:
            return self._block(
                symbol,
                f"max open positions reached ({open_count}/{MAX_OPEN_POSITIONS})",
            )

        if confidence < _MIN_CONFIDENCE:
            return self._block(
                symbol,
                f"confidence {confidence:.2f} < {_MIN_CONFIDENCE} threshold",
            )

        if sig == "SELL" and symbol not in held:
            return self._block(symbol, "SELL signal but no position to close")

        return True, "ok"

    @staticmethod
    def _block(symbol: str, reason: str) -> tuple[bool, str]:
        logger.info(f"[{symbol}] BLOCKED — {reason}")
        return False, reason


def _is_float(val) -> bool:
    try:
        f = float(val)
        return f == f
    except (TypeError, ValueError):
        return False


def _check_exhaustion(df: pd.DataFrame, symbol: str) -> tuple[bool, str]:
    if len(df) < _EXHAUSTION_BARS + 1:
        return False, ""

    closes = df["close"].iloc[-(_EXHAUSTION_BARS + 1):].tolist()
    moves = [closes[i + 1] - closes[i] for i in range(_EXHAUSTION_BARS)]

    if all(m > 0 for m in moves):
        return True, (
            f"exhaustion: {_EXHAUSTION_BARS} consecutive up bars without retracement"
        )
    if all(m < 0 for m in moves):
        return True, (
            f"exhaustion: {_EXHAUSTION_BARS} consecutive down bars without retracement"
        )
    return False, ""


def compute_signal_strength(df: pd.DataFrame) -> float:
    score = 0.0
    if df.empty:
        return score
    last = df.iloc[-1]

    rsi = last.get("rsi_14")
    if _is_float(rsi):
        rsi = float(rsi)
        score += 0.3 if rsi < 40 else (-0.3 if rsi > 60 else 0)

    if _is_float(last.get("macd")) and _is_float(last.get("macd_signal")):
        score += 0.3 if float(last["macd"]) > float(last["macd_signal"]) else -0.3

    if _is_float(last.get("ema_9")) and _is_float(last.get("ema_21")):
        score += 0.2 if float(last["ema_9"]) > float(last["ema_21"]) else -0.2

    vwap = last.get("vwap")
    if _is_float(vwap) and _is_float(last.get("close")):
        score += 0.2 if float(last["close"]) > float(vwap) else -0.2

    return max(-1.0, min(1.0, score))
