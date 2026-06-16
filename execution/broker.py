from __future__ import annotations

import uuid
from typing import Optional

import ccxt

from config.settings import (
    EXCHANGE_API_KEY,
    EXCHANGE_ID,
    EXCHANGE_SANDBOX,
    EXCHANGE_SECRET,
    MARKET_TYPE,
    TRADING_MODE,
)
from logs.logger import get_logger
from risk.manager import PositionSpec

logger = get_logger(__name__)

_SLIPPAGE = 0.0005


class BrokerError(Exception):
    def __init__(self, message: str, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.cause = cause


def _dry_order(symbol: str, **extras) -> dict:
    fake_id = f"dry-{uuid.uuid4().hex[:8]}"
    return {
        "id": fake_id,
        "order_id": fake_id,
        "symbol": symbol,
        "filled_qty": 0.0,
        "filled_avg_price": 0.0,
        "status": "dry_run",
        **extras,
    }


class CryptoBroker:
    """ccxt spot execution layer with dry-run support."""

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self._exchange: Optional[ccxt.Exchange] = None

        if hasattr(ccxt, EXCHANGE_ID):
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
                logger.info(
                    f"CryptoBroker: {EXCHANGE_ID} ({mode})"
                    + (" [DRY RUN]" if dry_run else "")
                )
            except Exception as exc:
                logger.warning(f"CryptoBroker init failed: {exc}")

    def _round_amount(self, symbol: str, amount: float) -> float:
        if not self._exchange:
            return round(amount, 8)
        try:
            return float(self._exchange.amount_to_precision(symbol, amount))
        except Exception:
            return round(amount, 8)

    def _round_price(self, symbol: str, price: float) -> float:
        if not self._exchange:
            return round(price, 8)
        try:
            return float(self._exchange.price_to_precision(symbol, price))
        except Exception:
            return round(price, 8)

    def submit_bracket_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        if qty <= 0:
            raise BrokerError(f"submit_bracket_order: qty must be > 0, got {qty}")

        is_buy = side.upper() in ("BUY", "LONG")
        limit_price = self._round_price(
            symbol,
            entry_price * (1 + _SLIPPAGE) if is_buy else entry_price * (1 - _SLIPPAGE),
        )
        qty = self._round_amount(symbol, qty)
        stop_loss = self._round_price(symbol, stop_loss)
        take_profit = self._round_price(symbol, take_profit)

        if is_buy:
            try:
                acct = self.get_account()
                free_usdt = float(acct.get("free_usdt", acct.get("cash", 0)))
            except BrokerError:
                free_usdt = float("inf")
            order_value = qty * limit_price
            if order_value > free_usdt:
                max_qty = self._round_amount(symbol, free_usdt / limit_price)
                if max_qty <= 0:
                    raise BrokerError(
                        f"{symbol}: insufficient USDT — need=${order_value:.2f}, "
                        f"free=${free_usdt:.2f}"
                    )
                logger.warning(
                    f"{symbol}: qty reduced {qty}→{max_qty} to fit free USDT"
                )
                qty = max_qty

        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy" if is_buy else "sell",
            "type": "limit",
            "limit_price": limit_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        logger.info(f"[ORDER PAYLOAD] {payload}")

        if self.dry_run:
            return _dry_order(
                symbol,
                qty=qty,
                side="BUY" if is_buy else "SELL",
            )

        self._require_client("submit_bracket_order")
        try:
            order_side = "buy" if is_buy else "sell"
            order = self._exchange.create_order(
                symbol=symbol,
                type="limit",
                side=order_side,
                amount=qty,
                price=limit_price,
            )
            order_id = str(order.get("id", ""))
            self._place_exit_orders(symbol, qty, stop_loss, take_profit, is_buy)
            return {
                "id": order_id,
                "order_id": order_id,
                "symbol": symbol,
                "qty": qty,
                "status": order.get("status", "open"),
            }
        except Exception as exc:
            raise BrokerError(f"submit_bracket_order failed for {symbol}", exc) from exc

    def _place_exit_orders(
        self,
        symbol: str,
        qty: float,
        stop_loss: float,
        take_profit: float,
        is_long: bool,
    ) -> None:
        if not self._exchange:
            return
        exit_side = "sell" if is_long else "buy"
        try:
            if self._exchange.has.get("createOrder", False):
                self._exchange.create_order(
                    symbol, "limit", exit_side, qty, take_profit,
                    params={"stopPrice": stop_loss} if self._exchange.has.get("stopLoss") else {},
                )
        except Exception as exc:
            logger.warning(
                f"{symbol}: OCO/bracket exit not placed ({exc}); "
                "bot will manage stops in scan loop"
            )

    def cancel_order(self, order_id: str, symbol: str = "") -> bool:
        if self.dry_run:
            return True
        self._require_client("cancel_order")
        try:
            self._exchange.cancel_order(order_id, symbol or None)
            return True
        except Exception as exc:
            raise BrokerError(f"cancel_order failed for {order_id}", exc) from exc

    def cancel_all_orders(self) -> int:
        if self.dry_run:
            return 0
        self._require_client("cancel_all_orders")
        try:
            if hasattr(self._exchange, "cancel_all_orders"):
                result = self._exchange.cancel_all_orders()
                return len(result) if result else 0
            open_orders = self._exchange.fetch_open_orders()
            count = 0
            for order in open_orders:
                self._exchange.cancel_order(order["id"], order.get("symbol"))
                count += 1
            return count
        except Exception as exc:
            raise BrokerError("cancel_all_orders failed", exc) from exc

    def close_position(self, symbol: str) -> dict:
        if self.dry_run:
            return _dry_order(symbol)

        self._require_client("close_position")
        try:
            base = symbol.split("/")[0]
            balance = self._exchange.fetch_balance()
            amount = float(balance.get("free", {}).get(base, 0) or 0)
            if amount <= 0:
                return {"id": "", "order_id": "", "symbol": symbol, "status": "no_position"}
            amount = self._round_amount(symbol, amount)
            order = self._exchange.create_order(symbol, "market", "sell", amount)
            oid = str(order.get("id", ""))
            return {"id": oid, "order_id": oid, "symbol": symbol, "status": "closed"}
        except Exception as exc:
            raise BrokerError(f"close_position failed for {symbol}", exc) from exc

    def get_open_orders(self) -> list[dict]:
        if not self._exchange:
            return []
        try:
            orders = self._exchange.fetch_open_orders()
            return [
                {
                    "id": str(o.get("id", "")),
                    "symbol": o.get("symbol", ""),
                    "side": o.get("side", ""),
                    "qty": float(o.get("amount") or 0),
                    "status": o.get("status", ""),
                }
                for o in orders
            ]
        except Exception as exc:
            raise BrokerError("get_open_orders failed", exc) from exc

    def close_all_positions_eod(self) -> None:
        if self.dry_run or not self._exchange:
            logger.info("[DRY RUN] Would close all positions at daily close")
            return
        try:
            balance = self._exchange.fetch_balance()
            for currency, amount in balance.get("total", {}).items():
                amt = float(amount or 0)
                if amt <= 0 or currency in ("USDT", "USD", "BUSD"):
                    continue
                symbol = f"{currency}/USDT"
                if symbol in self._exchange.markets:
                    try:
                        self.close_position(symbol)
                    except BrokerError as exc:
                        logger.error(f"Daily close failed for {symbol}: {exc}")
        except Exception as exc:
            raise BrokerError("close_all_positions_eod failed", exc) from exc

    def get_account(self) -> dict:
        if not self._exchange or not EXCHANGE_API_KEY:
            return {
                "equity": 100_000.0,
                "cash": 100_000.0,
                "buying_power": 100_000.0,
                "portfolio_value": 100_000.0,
                "free_usdt": 100_000.0,
            }
        try:
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
        except Exception as exc:
            raise BrokerError("get_account failed", exc) from exc

    def get_positions(self) -> list[dict]:
        if not self._exchange:
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
                ticker = self._exchange.fetch_ticker(symbol)
                price = float(ticker.get("last") or 0)
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
            raise BrokerError("get_positions failed", exc) from exc

    def submit_order(self, spec: PositionSpec) -> dict:
        try:
            return self.submit_bracket_order(
                symbol=spec.symbol,
                qty=spec.qty,
                side=spec.side,
                entry_price=spec.entry_price,
                stop_loss=spec.stop_loss,
                take_profit=spec.take_profit,
            )
        except BrokerError as exc:
            logger.error(f"submit_order: {exc}")
            return {"error": str(exc), "symbol": spec.symbol}

    def _require_client(self, method: str = "") -> None:
        if not self._exchange:
            raise BrokerError(
                f"{method + ': ' if method else ''}"
                "Exchange client not initialized — check EXCHANGE_API_KEY / EXCHANGE_SECRET"
            )
