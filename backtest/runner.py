"""Crypto intraday mean-reversion backtester.

Run a single pair:
    python -m backtest.runner BTC/USDT 2025-01-01 2025-06-01

Run the full watchlist (last 6 months):
    python -m backtest.runner
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from typing import Union

import ccxt
import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

from config.settings import (
    ATR_PERIOD,
    BB_PERIOD,
    BB_STD,
    EXCHANGE_ID,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    RSI_PERIOD,
)
from config.watchlist import WATCHLIST
from logs.logger import get_logger

logger = get_logger(__name__)
_RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def _ind_rsi(close: pd.Series, period: int) -> np.ndarray:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).to_numpy()


def _ind_macd_hist(close: pd.Series, fast: int, slow: int, signal: int) -> np.ndarray:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    sig_line = macd_line.ewm(span=signal, adjust=False).mean()
    return (macd_line - sig_line).to_numpy()


def _ind_bb_pct(close: pd.Series, period: int, std: float) -> np.ndarray:
    mid = close.rolling(period).mean()
    sigma = close.rolling(period).std(ddof=0)
    upper = mid + std * sigma
    lower = mid - std * sigma
    width = upper - lower
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(width > 0, (close.to_numpy() - lower.to_numpy()) / width.to_numpy(), np.nan)
    return pct


def _ind_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> np.ndarray:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean().to_numpy()


def _ind_vol_ratio(volume: pd.Series, period: int) -> np.ndarray:
    sma = volume.rolling(period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sma > 0, volume.to_numpy() / sma.to_numpy(), np.nan)


class CryptoDayTraderStrategy(Strategy):
    rsi_period: int = RSI_PERIOD
    macd_fast: int = MACD_FAST
    macd_slow: int = MACD_SLOW
    macd_sig: int = MACD_SIGNAL
    bb_period: int = BB_PERIOD
    bb_std: float = float(BB_STD)
    atr_period: int = ATR_PERIOD
    vol_sma_period: int = 20

    rsi_oversold: float = 40.0
    rsi_overbought: float = 60.0
    bb_lower_zone: float = 0.25
    bb_upper_zone: float = 0.75
    atr_sl_mult: float = 2.0
    rr_ratio: float = 2.0
    position_size_pct: float = 0.10
    max_atr_pct: float = 0.08
    min_vol_ratio: float = 0.5
    exhaustion_bars: int = 5

    def init(self) -> None:
        close = pd.Series(self.data.Close)
        high = pd.Series(self.data.High)
        low = pd.Series(self.data.Low)
        volume = pd.Series(self.data.Volume)

        self.i_rsi = self.I(_ind_rsi, close, self.rsi_period)
        self.i_macd_hist = self.I(
            _ind_macd_hist, close, self.macd_fast, self.macd_slow, self.macd_sig
        )
        self.i_bb_pct = self.I(_ind_bb_pct, close, self.bb_period, self.bb_std)
        self.i_atr = self.I(_ind_atr, high, low, close, self.atr_period)
        self.i_vol_ratio = self.I(_ind_vol_ratio, volume, self.vol_sma_period)

    def next(self) -> None:
        price = float(self.data.Close[-1])
        rsi = float(self.i_rsi[-1])
        hist = float(self.i_macd_hist[-1])
        hist_prev = float(self.i_macd_hist[-2]) if len(self.i_macd_hist) >= 2 else float("nan")
        bb_p = float(self.i_bb_pct[-1])
        atr = float(self.i_atr[-1])
        vol_ratio = float(self.i_vol_ratio[-1])

        if any(v != v for v in [rsi, hist, hist_prev, bb_p, atr, vol_ratio]):
            return
        if vol_ratio < self.min_vol_ratio:
            return
        if price > 0 and atr / price > self.max_atr_pct:
            return

        n = self.exhaustion_bars
        if len(self.data.Close) > n:
            tail = self.data.Close[-(n + 1):]
            moves = [float(tail[i + 1]) - float(tail[i]) for i in range(n)]
            if all(m > 0 for m in moves) or all(m < 0 for m in moves):
                return

        hist_turning_pos = hist > hist_prev
        hist_turning_neg = hist < hist_prev
        buy_signal = rsi < self.rsi_oversold and hist_turning_pos and bb_p < self.bb_lower_zone
        sell_signal = rsi > self.rsi_overbought and hist_turning_neg and bb_p > self.bb_upper_zone

        size = max(0.001, self.equity * self.position_size_pct / price)
        sl_dist = self.atr_sl_mult * atr
        tp_dist = sl_dist * self.rr_ratio

        if buy_signal and not self.position:
            self.buy(size=size, sl=round(price - sl_dist, 6), tp=round(price + tp_dist, 6))
        elif sell_signal and self.position.is_long:
            self.position.close()


def _download(symbol: str, start: str, end: str, timeframe: str = "1d") -> pd.DataFrame:
    if not hasattr(ccxt, EXCHANGE_ID):
        raise ValueError(f"Unknown exchange: {EXCHANGE_ID}")

    exchange_cls = getattr(ccxt, EXCHANGE_ID)
    exchange = exchange_cls({"enableRateLimit": True})
    exchange.load_markets()

    since = exchange.parse8601(f"{start}T00:00:00Z")
    end_ms = exchange.parse8601(f"{end}T00:00:00Z")
    all_bars: list = []

    while since < end_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        all_bars.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_bars, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp")
    df.index = df.index.tz_localize(None)
    return df.dropna()


def run_backtest(
    symbol: str,
    start_date: Union[str, date],
    end_date: Union[str, date],
    cash: float = 10_000,
    commission: float = 0.001,
) -> dict:
    start_s = str(start_date)
    end_s = str(end_date)
    logger.info(f"Backtest: {symbol} {start_s} → {end_s}")

    try:
        df = _download(symbol, start_s, end_s)
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}

    if df.empty or len(df) < 50:
        return {"symbol": symbol, "error": "insufficient data"}

    bt = Backtest(df, CryptoDayTraderStrategy, cash=cash, commission=commission)
    stats = bt.run()

    os.makedirs(_RESULTS_DIR, exist_ok=True)
    html_path = os.path.join(
        _RESULTS_DIR,
        f"{symbol.replace('/', '-')}_{start_s}_{end_s}.html",
    )
    try:
        bt.plot(filename=html_path, open_browser=False)
    except Exception:
        html_path = ""

    def _f(key: str, default: float = 0.0) -> float:
        try:
            v = float(stats[key])
            return default if v != v else v
        except (KeyError, TypeError, ValueError):
            return default

    return {
        "symbol": symbol,
        "start_date": start_s,
        "end_date": end_s,
        "total_return_pct": round(_f("Return [%]"), 2),
        "win_rate": round(_f("Win Rate [%]") / 100, 4),
        "max_drawdown_pct": round(abs(_f("Max. Drawdown [%]")), 2),
        "sharpe_ratio": round(_f("Sharpe Ratio"), 4),
        "num_trades": int(_f("# Trades")),
        "best_trade": round(_f("Best Trade [%]"), 2),
        "worst_trade": round(_f("Worst Trade [%]"), 2),
        "html_report": html_path,
    }


def run_all_watchlist_backtest(months: int = 6) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=months * 30)
    results = []
    for symbol in WATCHLIST:
        try:
            results.append(run_backtest(symbol, start, end))
        except Exception as exc:
            results.append({"symbol": symbol, "error": str(exc)})
    _print_summary(results)
    return results


def _print_summary(results: list[dict]) -> None:
    print("\nBacktest Summary")
    print("-" * 70)
    for r in results:
        if "error" in r:
            print(f"{r.get('symbol', '?')}: ERROR — {r['error']}")
        else:
            print(
                f"{r['symbol']}: return={r['total_return_pct']:+.2f}%  "
                f"trades={r['num_trades']}  win={r['win_rate']:.1%}  "
                f"dd={r['max_drawdown_pct']:.2f}%"
            )
    print()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        run_all_watchlist_backtest()
    elif len(sys.argv) == 4:
        _, sym, s, e = sys.argv
        res = run_backtest(sym, s, e)
        _print_summary([res])
    else:
        print("Usage:")
        print("  python -m backtest.runner")
        print("  python -m backtest.runner BTC/USDT 2025-01-01 2025-06-01")
        sys.exit(1)
