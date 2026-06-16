from __future__ import annotations

import json
import time
from typing import Optional

import anthropic

from config.settings import ANTHROPIC_MODEL
from logs.logger import get_logger

logger = get_logger(__name__)

_MAX_TOKENS = 512
_CACHE_TTL = 60

_SYSTEM = """\
You are a quantitative trading analyst specializing in intraday cryptocurrency spot markets.

Your job is to analyze technical indicator data for a single trading pair and produce
a precise, actionable trading signal. You must be selective: only issue BUY or
SELL when the evidence is strong and aligned across multiple indicators.

Context:
- Crypto markets trade 24/7 with no session open or close.
- Volatility is typically higher than equities; ATR% above 2% is common on alts.
- There are no earnings, dividends, or sector rotation events.
- Consider spread, 24h volume, and momentum alignment.

Rules:
- Respond ONLY with a single JSON object. No markdown fences, no explanation outside the JSON.
- "confidence" must reflect genuine conviction (< 0.6 → use HOLD).
- "reasoning" must be one sentence, max 20 words.
- "suggested_entry", "suggested_stop_loss", "suggested_take_profit" are floats or null.
- "risk_level" is LOW when ATR% < 1.0, HIGH when ATR% > 3.0, otherwise MEDIUM.

Required JSON schema (output nothing else):
{
  "signal": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "reasoning": "string",
  "suggested_entry": float | null,
  "suggested_stop_loss": float | null,
  "suggested_take_profit": float | null,
  "risk_level": "LOW" | "MEDIUM" | "HIGH"
}\
"""

_HOLD_DEFAULT: dict = {
    "signal": "HOLD",
    "confidence": 0.0,
    "reasoning": "Default — no signal generated",
    "suggested_entry": None,
    "suggested_stop_loss": None,
    "suggested_take_profit": None,
    "risk_level": "MEDIUM",
}


def _hold(reason: str) -> dict:
    return {**_HOLD_DEFAULT, "reasoning": reason}


def _build_user_message(
    symbol: str,
    indicator_summary: dict,
    price_summary: str,
) -> str:
    s = indicator_summary

    def _fmt(key: str, default: str = "n/a") -> str:
        val = s.get(key)
        if val is None or (isinstance(val, float) and val != val):
            return default
        if isinstance(val, float):
            return f"{val:.6f}"
        return str(val)

    ema_cross = (
        "EMA9 > EMA21 (bullish)" if s.get("ema_bullish") else "EMA9 < EMA21 (bearish)"
    )
    vwap_pos = (
        "above VWAP" if s.get("above_vwap") else
        "below VWAP" if s.get("above_vwap") is False else
        "VWAP unavailable"
    )
    macd_dir = (
        "bullish (MACD > signal)" if s.get("macd_bullish") else "bearish (MACD < signal)"
    )

    return f"""\
Pair: {symbol}
Current price: {_fmt("price")}

--- Indicator values ---
RSI(14):        {_fmt("rsi_14")}
MACD:           {_fmt("macd")}  Signal: {_fmt("macd_signal")}  Hist: {_fmt("macd_hist")}
MACD direction: {macd_dir}
BB%:            {_fmt("bb_pct")}  (0=lower band, 1=upper band)
  BB upper:     {_fmt("bb_upper")}
  BB lower:     {_fmt("bb_lower")}
EMA crossover:  {ema_cross}  (EMA9={_fmt("ema_9")}, EMA21={_fmt("ema_21")})
ATR(14):        {_fmt("atr_14")}  ({_fmt("atr_pct_price")}% of price)
Volume ratio:   {_fmt("volume_ratio")}x 20-bar average
VWAP:           {_fmt("vwap")}  — price is {vwap_pos}
24h quote vol:  {_fmt("quote_volume_24h", "n/a")} USDT
24h change:     {_fmt("percentage_24h", "n/a")}%

--- Market context ---
{price_summary}

Respond with the JSON signal object only.\
"""


def _parse_response(raw: str, symbol: str) -> Optional[dict]:
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(f"[{symbol}] JSON decode failed: {exc} | raw={raw!r}")
        return None

    signal = str(result.get("signal", "")).upper()
    if signal not in ("BUY", "SELL", "HOLD"):
        logger.error(f"[{symbol}] Invalid signal value: {signal!r}")
        return None

    try:
        confidence = float(result["confidence"])
    except (KeyError, TypeError, ValueError):
        logger.error(f"[{symbol}] Missing or invalid confidence field")
        return None

    def _opt_float(key: str) -> Optional[float]:
        val = result.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    risk = str(result.get("risk_level", "MEDIUM")).upper()
    if risk not in ("LOW", "MEDIUM", "HIGH"):
        risk = "MEDIUM"

    return {
        "signal": signal,
        "confidence": round(confidence, 4),
        "reasoning": str(result.get("reasoning", ""))[:120],
        "suggested_entry": _opt_float("suggested_entry"),
        "suggested_stop_loss": _opt_float("suggested_stop_loss"),
        "suggested_take_profit": _opt_float("suggested_take_profit"),
        "risk_level": risk,
    }


class AISignalGenerator:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._cache: dict[str, tuple[float, dict]] = {}

    def analyze(
        self,
        symbol: str,
        indicator_summary: dict,
        price_summary: str,
    ) -> dict:
        cached = self._get_cached(symbol)
        if cached is not None:
            logger.debug(f"[{symbol}] Returning cached signal (TTL={_CACHE_TTL}s)")
            return cached

        user_msg = _build_user_message(symbol, indicator_summary, price_summary)
        raw = self._call_claude(symbol, user_msg)

        if raw is None:
            result = _hold("API call failed")
        else:
            parsed = _parse_response(raw, symbol)
            if parsed is None:
                result = _hold("Response parse failed")
            elif parsed["confidence"] < 0.6:
                result = {**parsed, "signal": "HOLD"}
            else:
                result = parsed

        result["symbol"] = symbol
        self._set_cache(symbol, result)

        log_level = "info" if result["signal"] != "HOLD" else "debug"
        getattr(logger, log_level)(
            f"[{symbol}] signal={result['signal']} "
            f"confidence={result['confidence']:.2f} "
            f"risk={result['risk_level']} | {result['reasoning']}"
        )
        return result

    def batch_analyze(
        self,
        symbols: list[str],
        indicator_summaries: dict[str, dict],
        price_summaries: dict[str, str],
    ) -> list[dict]:
        results: list[dict] = []
        for symbol in symbols:
            ind = indicator_summaries.get(symbol)
            if ind is None:
                results.append({**_hold("No indicator data"), "symbol": symbol})
                continue
            price_text = price_summaries.get(symbol, "No market context available.")
            results.append(self.analyze(symbol, ind, price_text))
        results.sort(key=lambda r: r["confidence"], reverse=True)
        return results

    def _call_claude(self, symbol: str, user_msg: str) -> Optional[str]:
        try:
            response = self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            for block in response.content:
                if block.type == "text":
                    return block.text.strip()
            return None
        except anthropic.RateLimitError as exc:
            logger.error(f"[{symbol}] Rate limit hit: {exc}")
        except anthropic.APIStatusError as exc:
            logger.error(f"[{symbol}] API error {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError as exc:
            logger.error(f"[{symbol}] Connection error: {exc}")
        except Exception as exc:
            logger.error(f"[{symbol}] Unexpected error: {exc}")
        return None

    def _get_cached(self, symbol: str) -> Optional[dict]:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        ts, result = entry
        if time.monotonic() - ts > _CACHE_TTL:
            del self._cache[symbol]
            return None
        return result

    def _set_cache(self, symbol: str, result: dict) -> None:
        self._cache[symbol] = (time.monotonic(), result)
