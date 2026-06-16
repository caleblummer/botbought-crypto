from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import ccxt
import pandas as pd

from config.settings import (
    EXCHANGE_API_KEY,
    EXCHANGE_ID,
    EXCHANGE_SANDBOX,
    EXCHANGE_SECRET,
    MARKET_TYPE,
)
from logs.logger import get_logger

logger = get_logger(__name__)

_RETRY_DELAYS = (1, 2, 4)


def _with_retry(fn, label: str):
    last_exc: Exception | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < len(_RETRY_DELAYS):
                logger.warning(
                    f"{label}: attempt {attempt} failed ({exc}); retrying in {delay}s"
                )
                time.sleep(delay)
            else:
                logger.error(f"{label}: all retries exhausted — {exc}")
    raise last_exc


def _normalize_ohlcv(raw: list, symbol: str) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()

    df = pd.DataFrame(
        raw,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df.index.name = "timestamp"
    df = df.astype(float)
    return df.sort_index()


class CryptoDataFetcher:
    """Market data via ccxt — OHLCV, tickers, balances."""

    def __init__(self) -> None:
        self._exchange: Optional[ccxt.Exchange] = None
        self._init_exchange()

    def _init_exchange(self) -> None:
        if not hasattr(ccxt, EXCHANGE_ID):
            logger.warning(f"Unknown exchange id {EXCHANGE_ID!r}")
            return

        exchange_cls = getattr(ccxt, EXCHANGE_ID)
        config: dict = {
            "enableRateLimit": True,
            "options": {"defaultType": MARKET_TYPE},
        }
        if EXCHANGE_API_KEY:
            config["apiKey"] = EXCHANGE_API_KEY
            config["secret"] = EXCHANGE_SECRET

        try:
            self._exchange = exchange_cls(config)
            if EXCHANGE_SANDBOX and hasattr(self._exchange, "set_sandbox_mode"):
                self._exchange.set_sandbox_mode(True)
            self._exchange.load_markets()
            mode = "sandbox" if EXCHANGE_SANDBOX else "live"
            logger.info(f"ccxt {EXCHANGE_ID} ready ({mode}, {MARKET_TYPE})")
        except Exception as exc:
            logger.error(f"Exchange init failed: {exc}")
            self._exchange = None

    @property
    def exchange(self) -> Optional[ccxt.Exchange]:
        return self._exchange

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 100,
    ) -> pd.DataFrame:
        if not self._exchange:
            return pd.DataFrame()

        def _fetch():
            raw = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            return _normalize_ohlcv(raw, symbol).tail(limit)

        try:
            return _with_retry(_fetch, label=f"get_bars({symbol})")
        except Exception as exc:
            logger.warning(f"get_bars({symbol}) failed: {exc}")
            return pd.DataFrame()

    def get_latest_quote(self, symbol: str) -> dict:
        if not self._exchange:
            return {"bid": 0.0, "ask": 0.0, "mid_price": 0.0, "spread": 0.0}

        def _fetch():
            ticker = self._exchange.fetch_ticker(symbol)
            bid = float(ticker.get("bid") or ticker.get("last") or 0)
            ask = float(ticker.get("ask") or ticker.get("last") or bid)
            mid = round((bid + ask) / 2, 8) if bid and ask else float(ticker.get("last") or 0)
            spread = round(ask - bid, 8) if bid and ask else 0.0
            return {
                "bid": bid,
                "ask": ask,
                "mid_price": mid,
                "spread": spread,
                "quote_volume_24h": float(ticker.get("quoteVolume") or 0),
                "percentage_24h": float(ticker.get("percentage") or 0),
            }

        try:
            return _with_retry(_fetch, label=f"get_latest_quote({symbol})")
        except Exception as exc:
            logger.warning(f"get_latest_quote({symbol}) failed: {exc}")
            return {"bid": 0.0, "ask": 0.0, "mid_price": 0.0, "spread": 0.0}

    def get_latest_trade(self, symbol: str) -> dict:
        quote = self.get_latest_quote(symbol)
        return {
            "price": quote["mid_price"],
            "size": 0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_account(self) -> dict:
        if not self._exchange or not EXCHANGE_API_KEY:
            return {
                "equity": 100_000.0,
                "cash": 100_000.0,
                "buying_power": 100_000.0,
                "portfolio_value": 100_000.0,
                "free_usdt": 100_000.0,
            }

        def _fetch():
            balance = self._exchange.fetch_balance()
            free = balance.get("free", {})
            total = balance.get("total", {})
            usdt_free = float(free.get("USDT") or free.get("USD") or 0)
            usdt_total = float(total.get("USDT") or total.get("USD") or usdt_free)
            return {
                "equity": usdt_total,
                "cash": usdt_free,
                "buying_power": usdt_free,
                "portfolio_value": usdt_total,
                "free_usdt": usdt_free,
            }

        try:
            return _with_retry(_fetch, label="get_account()")
        except Exception as exc:
            logger.warning(f"get_account() failed: {exc}")
            return {
                "equity": 0.0,
                "cash": 0.0,
                "buying_power": 0.0,
                "portfolio_value": 0.0,
                "free_usdt": 0.0,
            }

    def get_positions(self) -> list[dict]:
        """Return non-zero base-currency balances as pseudo-positions."""
        if not self._exchange or not EXCHANGE_API_KEY:
            return []

        try:
            balance = self._exchange.fetch_balance()
            total = balance.get("total", {})
            positions = []
            for currency, amount in total.items():
                amt = float(amount or 0)
                if amt <= 0 or currency in ("USDT", "USD", "BUSD"):
                    continue
                symbol = f"{currency}/USDT"
                if symbol not in self._exchange.markets:
                    continue
                ticker = self.get_latest_quote(symbol)
                price = ticker["mid_price"]
                positions.append({
                    "symbol": symbol,
                    "qty": amt,
                    "side": "long",
                    "avg_entry_price": price,
                    "market_value": amt * price,
                    "unrealized_pl": 0.0,
                    "unrealized_plpc": 0.0,
                })
            return positions
        except Exception as exc:
            logger.warning(f"get_positions() failed: {exc}")
            return []
