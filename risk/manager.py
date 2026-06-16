from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from config.settings import MAX_DAILY_LOSS_PCT, MAX_PORTFOLIO_RISK_PCT
from db.database import get_starting_equity, set_starting_equity
from logs.logger import get_logger

logger = get_logger(__name__)

_MAX_POSITION_PCT = 0.20
_ATR_STOP_MULT = 2.0
_ATR_TRAIL_MULT = 1.5
_FALLBACK_STOP_PCT = 0.02
_MIN_QTY = 0.0001


@dataclass
class PositionSpec:
    symbol: str
    side: str
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float
    notional: float


class RiskManager:
    def __init__(self, portfolio_value: float) -> None:
        today = date.today()
        stored = get_starting_equity(today)
        if stored is not None:
            self._starting_equity = stored
        else:
            self._starting_equity = portfolio_value
            set_starting_equity(today, portfolio_value)
        self._portfolio_value = portfolio_value

    def update_portfolio_value(self, value: float) -> None:
        self._portfolio_value = value

    def reset_day(self, portfolio_value: float) -> None:
        self._starting_equity = portfolio_value
        self._portfolio_value = portfolio_value
        set_starting_equity(date.today(), portfolio_value)

    def calculate_position_size(
        self,
        portfolio_value: float,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        if entry_price <= 0:
            return 0.0

        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit <= 0:
            return 0.0

        risk_amount = portfolio_value * MAX_PORTFOLIO_RISK_PCT
        qty = risk_amount / risk_per_unit

        max_qty = portfolio_value * _MAX_POSITION_PCT / entry_price
        qty = min(qty, max_qty)

        if qty < _MIN_QTY:
            return 0.0
        return qty

    def check_daily_loss_limit(
        self,
        starting_equity: float,
        current_equity: float,
    ) -> bool:
        authoritative_start = self._starting_equity or starting_equity
        if authoritative_start <= 0:
            return False
        loss_pct = (authoritative_start - current_equity) / authoritative_start
        return loss_pct > MAX_DAILY_LOSS_PCT

    @staticmethod
    def calculate_stop_loss(entry: float, atr: float, direction: str) -> float:
        if direction.upper() == "BUY":
            stop = entry - (_ATR_STOP_MULT * atr)
        else:
            stop = entry + (_ATR_STOP_MULT * atr)
        return round(stop, 8)

    @staticmethod
    def calculate_take_profit(
        entry: float,
        stop_loss: float,
        rr_ratio: float = 2.0,
    ) -> float:
        risk = abs(entry - stop_loss)
        if entry > stop_loss:
            return round(entry + rr_ratio * risk, 8)
        return round(entry - rr_ratio * risk, 8)

    @staticmethod
    def get_trailing_stop_update(
        current_price: float,
        highest_price: float,
        atr: float,
    ) -> float:
        return round(highest_price - (_ATR_TRAIL_MULT * atr), 8)

    def size_position(
        self,
        symbol: str,
        entry_price: float,
        side: str = "buy",
        confidence: float = 1.0,
        atr: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> Optional[PositionSpec]:
        if entry_price <= 0:
            return None

        direction = side.upper()

        if stop_loss_price is not None and _stop_is_valid(stop_loss_price, entry_price, direction):
            stop_loss = stop_loss_price
        elif atr is not None and atr > 0:
            stop_loss = self.calculate_stop_loss(entry_price, atr, direction)
        else:
            pct = _FALLBACK_STOP_PCT if direction == "BUY" else -_FALLBACK_STOP_PCT
            stop_loss = round(entry_price * (1 - pct), 8)

        effective_portfolio = self._portfolio_value * max(0.0, min(1.0, confidence))
        qty = self.calculate_position_size(effective_portfolio, entry_price, stop_loss)
        if qty < _MIN_QTY:
            return None

        if take_profit_price is not None and _tp_is_valid(take_profit_price, entry_price, direction):
            take_profit = take_profit_price
        else:
            take_profit = self.calculate_take_profit(entry_price, stop_loss)

        notional = round(qty * entry_price, 2)
        return PositionSpec(
            symbol=symbol,
            side=side.lower(),
            qty=qty,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            notional=notional,
        )


def _stop_is_valid(stop: float, entry: float, direction: str) -> bool:
    if direction == "BUY":
        return 0 < stop < entry
    return stop > entry > 0


def _tp_is_valid(tp: float, entry: float, direction: str) -> bool:
    if direction == "BUY":
        return tp > entry
    return 0 < tp < entry
