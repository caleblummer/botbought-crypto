from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from db.database import (
    db_close_trade,
    db_compute_all_time_stats,
    db_get_open_trades,
    db_get_today_trades,
    db_insert_trade,
    db_upsert_daily_stats,
    get_starting_equity,
    _compute_max_drawdown_from_snapshots,
)
from logs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float
    order_id: str
    trade_id: Optional[int] = None
    highest_price: Optional[float] = None
    opened_at: datetime = field(default_factory=datetime.utcnow)

    def current_pnl(self, price: float) -> float:
        if self.side == "buy":
            return (price - self.entry_price) * self.qty
        return (self.entry_price - price) * self.qty

    def pnl_pct(self, price: float) -> float:
        cost_basis = self.entry_price * self.qty
        if cost_basis == 0:
            return 0.0
        return self.current_pnl(price) / cost_basis

    def should_stop_loss(self, price: float) -> bool:
        if self.side == "buy":
            return price <= self.stop_loss
        return price >= self.stop_loss

    def should_take_profit(self, price: float) -> bool:
        if self.side == "buy":
            return price >= self.take_profit
        return price <= self.take_profit

    def update_highest_price(self, price: float) -> None:
        if self.side == "buy":
            if self.highest_price is None or price > self.highest_price:
                self.highest_price = price
        else:
            if self.highest_price is None or price < self.highest_price:
                self.highest_price = price


class PortfolioTracker:
    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    def add_position(self, pos: Position) -> None:
        self._positions[pos.symbol] = pos

    def remove_position(self, symbol: str) -> Optional[Position]:
        return self._positions.pop(symbol, None)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def open_count(self) -> int:
        return len(self._positions)

    def check_exits(self, prices: dict[str, float]) -> list[tuple[Position, str]]:
        exits = []
        for pos in list(self._positions.values()):
            price = prices.get(pos.symbol)
            if price is None:
                continue
            pos.update_highest_price(price)
            if pos.should_stop_loss(price):
                exits.append((pos, "stop_loss"))
            elif pos.should_take_profit(price):
                exits.append((pos, "take_profit"))
        return exits


class Portfolio:
    @staticmethod
    def get_available_cash(acct: dict) -> float:
        val = acct.get("free_usdt")
        if val is None:
            val = acct.get("cash", 0.0)
        return float(val)

    def record_trade_open(self, trade_dict: dict) -> int:
        return db_insert_trade(trade_dict)

    def record_trade_close(
        self,
        trade_id: int,
        exit_price: float,
        exit_time: Optional[datetime] = None,
        status: str = "closed",
    ) -> dict:
        if exit_time is None:
            exit_time = datetime.utcnow()

        open_trades = db_get_open_trades()
        trade = next((t for t in open_trades if t["id"] == trade_id), None)
        if trade is None:
            return {"trade_id": trade_id, "error": "trade not found"}

        entry_price = float(trade["entry_price"] or 0)
        qty = float(trade["qty"] or 0)
        side = str(trade["side"] or "buy")

        if side == "buy":
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty

        cost_basis = entry_price * qty
        pnl_pct = pnl / cost_basis if cost_basis != 0 else 0.0

        db_close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_time=exit_time,
            pnl=pnl,
            pnl_pct=pnl_pct,
            status=status,
        )

        return {
            "trade_id": trade_id,
            "symbol": trade["symbol"],
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct * 100, 3),
            "status": status,
        }

    def get_open_trades(self) -> list[dict]:
        return db_get_open_trades()

    def get_today_stats(self) -> dict:
        today_trades = db_get_today_trades()
        closed = [t for t in today_trades if t["status"] not in ("open",)]
        open_count = sum(1 for t in today_trades if t["status"] == "open")
        pnls = [float(t["pnl"]) for t in closed if t.get("pnl") is not None]
        total = len(closed)
        won = sum(1 for p in pnls if p > 0)
        starting = get_starting_equity(date.today()) or 0.0
        return {
            "date": date.today().isoformat(),
            "starting_equity": starting,
            "trades_taken": total,
            "trades_open": open_count,
            "trades_won": won,
            "trades_lost": total - won,
            "win_rate": round(won / total, 4) if total > 0 else 0.0,
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / total, 2) if total > 0 else 0.0,
        }

    def get_all_time_stats(self) -> dict:
        return db_compute_all_time_stats()

    def update_daily_stats(self, ending_equity: float, starting_equity: float) -> None:
        today_trades = db_get_today_trades()
        closed = [t for t in today_trades if t["status"] not in ("open",)]
        pnls = [float(t["pnl"]) for t in closed if t.get("pnl") is not None]
        total = len(closed)
        won = sum(1 for p in pnls if p > 0)
        max_dd = _compute_max_drawdown_from_snapshots()
        db_upsert_daily_stats({
            "date": date.today(),
            "starting_equity": round(starting_equity, 2),
            "ending_equity": round(ending_equity, 2),
            "trades_taken": total,
            "trades_won": won,
            "trades_lost": total - won,
            "total_pnl": round(sum(pnls), 4),
            "max_drawdown_pct": round(max_dd, 6),
        })
